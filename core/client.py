"""Асинхронный HTTP-клиент к Lamoda Seller Partner API.

Обязанности:
- взять креды из активного кабинета или окружения (никогда из кода/аргументов);
- получить и держать актуальным access-токен (JSON-RPC, авто-рефреш);
- выполнить запрос, описанный записью каталога, обернув его в конверт JSON-RPC;
- повторить при 429 с экспоненциальной задержкой;
- вернуть распакованный `result` либо канонический конверт ошибки.
"""
from __future__ import annotations

import asyncio
import email.utils
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from .credentials import FIELDS, CredentialStore
from .errors import classify_status, error_from_exception, make_error
from .jsonrpc import build_envelope, unwrap
from .registry import EndpointSpec

DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 4
BACKOFF_BASE = 1.5  # секунды, умножается на 2**попытка

# Публичный шлюз. В самой спеке указан внутренний хост
# seller-gateway.service.lamoda.tech — он снаружи недоступен и НЕ используется.
DEFAULT_HOST = "public-api-seller.lamoda.ru"
BASE_PATH = "/jsonrpc"

TOKEN_CREATE_METHOD = "v1.tokens.create"
TOKEN_REFRESH_METHOD = "v1.tokens.update"

# Обновляем токен за столько секунд до истечения, чтобы запрос в полёте не
# налетел на границу протухания.
TOKEN_EXPIRY_SKEW = 60.0
# Если Lamoda не вернула expiresIn, берём консервативные 15 минут: документация
# заявляет TTL 15 минут, пример в спеке — 86400. Меньшее из двух безопаснее,
# лишний рефреш дешевле протухшего токена.
FALLBACK_TOKEN_TTL = 900.0


@dataclass
class ServiceConfig:
    """Обвязка сервиса: всё специфичное для Lamoda собрано в одном месте."""

    name: str = "lamoda"
    scheme: str = "https"
    host: str = DEFAULT_HOST
    base_path: str = BASE_PATH
    fields: list[str] = field(default_factory=lambda: list(FIELDS))
    store: CredentialStore = field(default_factory=CredentialStore)
    user_agent: str = "lamoda-mcp-ru/0.1 (+https://github.com/ilyautov/lamoda-mcp-ru)"
    # Разрешённые хосты. Запрос уходит только на хост, равный одному из этих
    # имён или оканчивающийся на них по границе точки. Это не даёт агенту,
    # уведённому промпт-инъекцией, отправить заголовок авторизации на чужой хост
    # через call_raw.
    allowed_host_suffixes: list[str] = field(default_factory=lambda: [".lamoda.ru", ".lamoda.tech"])

    def resolve_creds(self) -> tuple[dict[str, str], str]:
        return self.store.resolve(self.fields)

    def missing_creds(self) -> list[str]:
        return self.store.missing(self.fields)

    def host_allowed(self, host: str) -> bool:
        """True, если хост входит в allowlist.

        Совпадение по суффиксу привязано к границе точки, поэтому
        `public-api-seller.lamoda.ru.evil.com` НЕ пройдёт как `.lamoda.ru`.
        """
        if not self.allowed_host_suffixes:
            return True
        h = host.strip().lower()
        for pre in ("https://", "http://"):
            if h.startswith(pre):
                h = h[len(pre):]
        h = h.strip("/").split("/")[0].split(":")[0]
        for suf in self.allowed_host_suffixes:
            bare = suf.lstrip(".")
            if h == bare or h.endswith("." + bare):
                return True
        return False


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        return float(value)  # число секунд
    except ValueError:
        pass
    # Форма HTTP-date. parsedate_to_datetime на мусоре бросает исключение,
    # поэтому оборачиваем: плохой заголовок не должен ронять запрос.
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (ValueError, TypeError):
        return None
    if dt is None:
        return None
    return max(0.0, dt.timestamp() - time.time())


class LamodaClient:
    def __init__(self, config: Optional[ServiceConfig] = None,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.config = config or ServiceConfig()
        # Кэш токена на набор кредов, чтобы переключение кабинета не переиспользовало
        # чужой токен: {ключ_кредов: (access_token, refresh_token, монотонное_истечение)}
        self._tokens: dict[str, tuple[str, str, float]] = {}
        self._token_lock = asyncio.Lock()
        # Транспорт подменяется в тестах (httpx.MockTransport).
        self._transport = transport

    # --- вспомогательное -----------------------------------------------------
    def _client(self, timeout: float) -> httpx.AsyncClient:
        if self._transport is not None:
            return httpx.AsyncClient(timeout=timeout, transport=self._transport)
        return httpx.AsyncClient(timeout=timeout)

    @staticmethod
    def _creds_key(fields: list[str], creds: dict[str, str]) -> str:
        raw = json.dumps({f: creds.get(f, "") for f in fields}, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _creds_or_error(self) -> tuple[Optional[dict[str, str]], Optional[dict]]:
        creds, _source = self.config.resolve_creds()
        missing = [f for f in self.config.fields if not creds.get(f)]
        if missing:
            return None, make_error(
                "auth",
                f"Не хватает кредов: {', '.join(missing)}. Добавьте кабинет через "
                "lamoda_add_cabinet, запустите install.py или задайте переменные "
                "окружения LAMODA_CLIENT_ID и LAMODA_CLIENT_SECRET.",
                retryable=False,
            )
        return creds, None

    def _url(self, path: str, host: Optional[str] = None) -> str:
        h = (host or self.config.host).strip()
        for pre in ("https://", "http://"):
            if h.startswith(pre):
                h = h[len(pre):]
        h = h.strip("/")
        return f"{self.config.scheme}://{h}{path}"

    def method_path(self, rpc_method: str) -> str:
        """Запасной способ получить путь по имени метода.

        Используется ТОЛЬКО когда метода нет в каталоге (call_raw по имени,
        которого мы ещё не знаем). Для методов каталога путь берётся из поля
        `path` — он записан из спеки и не выводится из имени.

        Почему выводить нельзя: у части методов путь содержит вложенные
        сегменты ('/v1/fbo/warehouse.list', '/v1/supplier-return/item.list'),
        и обратное преобразование из точечного имени дало бы
        '/jsonrpc/v1/fbo.warehouse.list' — существующий, но ДРУГОЙ адрес.
        Эту ошибку поймал тест согласованности каталога.
        """
        head, _, tail = rpc_method.partition(".")
        if not tail:
            return f"{self.config.base_path}/{head}"
        return f"{self.config.base_path}/{head}/{tail}"

    # --- токен ---------------------------------------------------------------
    async def _ensure_token(self, creds: dict[str, str]) -> tuple[Optional[str], Optional[dict]]:
        """Вернуть (access_token, None) либо (None, конверт ошибки).

        Пока токен жив, отдаётся из кэша. Лок не даёт нескольким параллельным
        вызовам устроить лавину запросов за токеном.
        """
        key = self._creds_key(self.config.fields, creds)
        cached = self._tokens.get(key)
        if cached and time.monotonic() < cached[2]:
            return cached[0], None
        async with self._token_lock:
            # Перепроверка внутри лока: пока ждали, кто-то мог обновить.
            cached = self._tokens.get(key)
            if cached and time.monotonic() < cached[2]:
                return cached[0], None
            # Сначала пробуем дешёвый рефреш, если есть refreshToken.
            if cached and cached[1]:
                token, err = await self._refresh_token(cached[1], key)
                if token:
                    return token, None
                # Рефреш не удался (протух/отозван) — падаем обратно на полный вход.
            return await self._create_token(creds, key)

    async def _rpc_raw(self, method: str, params: dict[str, Any],
                       *, timeout: float = DEFAULT_TIMEOUT,
                       headers: Optional[dict[str, str]] = None) -> dict:
        """Один JSON-RPC вызов без авторизации и ретраев — для операций с токеном."""
        path = self.method_path(method)
        url = self._url(path)
        body = build_envelope(method, params)
        hdrs = {
            "User-Agent": self.config.user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        hdrs.update(headers or {})
        try:
            async with self._client(timeout) as client:
                resp = await client.post(url, json=body, headers=hdrs)
        except Exception as exc:  # noqa: BLE001
            return error_from_exception(exc, operation_id=method, endpoint=path)

        if not resp.is_success:
            etype, retryable = classify_status(resp.status_code)
            return make_error(
                etype,
                f"Эндпоинт токена Lamoda вернул {resp.status_code}: {_short_body(resp)}",
                code=resp.status_code,
                operation_id=method,
                endpoint=path,
                retryable=retryable,
            )
        # Ошибка получения токена тоже приезжает с HTTP 200 внутри конверта.
        return unwrap(_parse_body(resp), operation_id=method, endpoint=path,
                      status=resp.status_code)

    def _store_token(self, key: str, result: Any,
                     operation_id: str) -> tuple[Optional[str], Optional[dict]]:
        """Разобрать TokenResponse и положить в кэш."""
        if not isinstance(result, dict) or not result.get("accessToken"):
            return None, make_error(
                "auth",
                f"В ответе Lamoda нет accessToken: {str(result)[:200]}",
                operation_id=operation_id,
                retryable=False,
            )
        access = str(result["accessToken"])
        refresh = str(result.get("refreshToken") or "")
        try:
            ttl = float(result.get("expiresIn", FALLBACK_TOKEN_TTL))
        except (TypeError, ValueError):
            ttl = FALLBACK_TOKEN_TTL
        # Документация обещает 15 минут, пример в спеке — 86400. Реальное
        # значение берём из ответа и обновляемся с запасом.
        expiry = time.monotonic() + max(30.0, ttl - TOKEN_EXPIRY_SKEW)
        self._tokens[key] = (access, refresh, expiry)
        return access, None

    async def _create_token(self, creds: dict[str, str],
                            key: str) -> tuple[Optional[str], Optional[dict]]:
        # Имена полей в спеке — camelCase, в отличие от snake_case наших кредов.
        resp = await self._rpc_raw(
            TOKEN_CREATE_METHOD,
            {
                "clientId": creds.get("client_id", ""),
                "clientSecret": creds.get("client_secret", ""),
                "grantType": "client_credentials",
            },
        )
        if not resp.get("ok"):
            return None, resp
        return self._store_token(key, resp.get("data"), TOKEN_CREATE_METHOD)

    async def _refresh_token(self, refresh_token: str,
                             key: str) -> tuple[Optional[str], Optional[dict]]:
        resp = await self._rpc_raw(TOKEN_REFRESH_METHOD, {"refreshToken": refresh_token})
        if not resp.get("ok"):
            return None, resp
        return self._store_token(key, resp.get("data"), TOKEN_REFRESH_METHOD)

    def _invalidate_token(self, creds: dict[str, str]) -> None:
        self._tokens.pop(self._creds_key(self.config.fields, creds), None)

    # --- основной запрос -----------------------------------------------------
    async def call(
        self,
        rpc_method: str,
        params: Optional[dict[str, Any]] = None,
        *,
        operation_id: Optional[str] = None,
        path: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        retry_safe: bool = False,
        dry_run: bool = False,
        creds_override: Optional[dict[str, str]] = None,
    ) -> dict:
        """Выполнить один метод JSON-RPC.

        `path` задаётся вызывающим для методов каталога (там он взят из спеки).
        Без него путь восстанавливается из имени — годится только для методов,
        которых в каталоге нет.

        Успех -> {"ok": True, "status": int, "data": <содержимое result>}
        Ошибка -> канонический конверт (ok=False).

        `retry_safe` разрешает повтор при таймауте фазы чтения. Ставится только
        для методов, помеченных в каталоге как read: у Lamoda ВСЕ методы POST,
        поэтому слепой повтор мог бы вторично применить запись (например, дважды
        изменить цену). Повтор на фазе соединения безопасен всегда — запрос
        заведомо не дошёл до сервера.

        `dry_run` возвращает точное тело, которое ушло бы, ничего не отправляя.
        """
        op = operation_id or rpc_method
        path = path or self.method_path(rpc_method)

        if not self.config.host_allowed(self.config.host):
            return make_error(
                "forbidden",
                f"Хост {self.config.host!r} не входит в allowlist "
                f"({', '.join(self.config.allowed_host_suffixes)}). Запрос отклонён "
                "до отправки, чтобы креды не ушли на недоверенный хост.",
                operation_id=op, endpoint=path, retryable=False,
            )

        envelope = build_envelope(rpc_method, params)

        if dry_run:
            # Ничего не отправляем: показываем ровно то, что ушло бы. Нужно,
            # чтобы проверить форму params до первого реального вызова —
            # каталог собран из спеки и боем не подтверждён.
            return {
                "ok": True,
                "dry_run": True,
                "http_call_skipped": True,
                "request": {
                    "method": "POST",
                    "url": self._url(path),
                    "headers": {
                        "Authorization": "Bearer <токен подставляется при реальном вызове>",
                        "Content-Type": "application/json",
                    },
                    "body": envelope,
                },
                "note": "Запрос НЕ отправлялся. Уберите dry_run, чтобы выполнить.",
            }

        if creds_override is not None:
            creds, err = creds_override, None
        else:
            creds, err = self._creds_or_error()
        if err:
            return err

        token, terr = await self._ensure_token(creds or {})
        if terr:
            return terr

        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
            # tokenType в ответе Lamoda — "bearer".
            "Authorization": f"Bearer {token}",
        }
        url = self._url(path)

        attempt = 0
        refreshed_once = False
        while True:
            try:
                async with self._client(timeout) as client:
                    resp = await client.post(url, json=envelope, headers=headers)
            except Exception as exc:  # noqa: BLE001
                connect_phase = isinstance(
                    exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
                )
                read_phase = isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout))
                if attempt < MAX_RETRIES and (connect_phase or (read_phase and retry_safe)):
                    await asyncio.sleep(BACKOFF_BASE * (2**attempt))
                    attempt += 1
                    continue
                return error_from_exception(exc, operation_id=op, endpoint=path)

            if resp.status_code == 429 and attempt < MAX_RETRIES:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                delay = retry_after if retry_after is not None else BACKOFF_BASE * (2**attempt)
                await asyncio.sleep(min(delay, 60.0))
                attempt += 1
                continue

            if not resp.is_success:
                # Транспортный/шлюзовый сбой: до слоя JSON-RPC не дошло.
                if resp.status_code == 401 and not refreshed_once:
                    self._invalidate_token(creds or {})
                    token, terr = await self._ensure_token(creds or {})
                    if terr:
                        return terr
                    headers["Authorization"] = f"Bearer {token}"
                    refreshed_once = True
                    continue
                etype, retryable = classify_status(resp.status_code)
                return make_error(
                    etype,
                    f"Lamoda вернула HTTP {resp.status_code}: {_short_body(resp)}",
                    code=resp.status_code,
                    operation_id=op,
                    endpoint=path,
                    retryable=retryable,
                    retry_after_seconds=_parse_retry_after(resp.headers.get("Retry-After")),
                    details=_capped_details(resp),
                )

            result = unwrap(_parse_body(resp), operation_id=op, endpoint=path,
                            status=resp.status_code)

            # Протухший токен внутри конверта: HTTP 200, но error про авторизацию.
            # Один раз обновляемся и повторяем — это чтение состояния, не запись.
            if (not result.get("ok") and result.get("error_type") == "auth"
                    and not refreshed_once):
                self._invalidate_token(creds or {})
                token, terr = await self._ensure_token(creds or {})
                if terr:
                    return terr
                headers["Authorization"] = f"Bearer {token}"
                refreshed_once = True
                continue

            return result

    async def call_spec(
        self,
        spec: EndpointSpec,
        params: Optional[dict[str, Any]] = None,
        *,
        dry_run: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> dict:
        """Вызвать метод по записи каталога.

        Путь берётся из каталога (записан из спеки), а не восстанавливается из
        имени: у части методов путь содержит вложенные сегменты, и вывод из
        имени дал бы другой адрес.
        """
        return await self.call(
            spec.rpc_method,
            params,
            operation_id=spec.operation_id,
            path=spec.path,
            timeout=timeout,
            retry_safe=(spec.safety == "read"),
            dry_run=dry_run,
        )


def _parse_body(resp: httpx.Response) -> Any:
    ctype = resp.headers.get("Content-Type", "")
    if "application/json" in ctype:
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return resp.text
    if ctype.startswith(("image/", "application/pdf")):
        return {"_binary": True, "content_type": ctype, "bytes": len(resp.content)}
    # Content-Type может отсутствовать — пробуем разобрать как JSON,
    # иначе отдаём текст.
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return resp.text


def _short_body(resp: httpx.Response, limit: int = 300) -> str:
    body = _parse_body(resp)
    s = body if isinstance(body, str) else str(body)
    return s[:limit]


def _capped_details(resp: httpx.Response, limit: int = 2000) -> Any:
    """Детали ошибки с ограничением размера, чтобы гигантская HTML-страница
    не залила контекст агента."""
    body = _parse_body(resp)
    if isinstance(body, str) and len(body) > limit:
        return body[:limit] + f"... [обрезано {len(body) - limit} символов]"
    return body
