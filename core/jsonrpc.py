"""Слой конверта JSON-RPC 2.0 — сборка запроса и разбор ответа Lamoda.

Зачем отдельный модуль. У WB и Ozon транспорт REST: успех/неуспех виден по
HTTP-статусу. У Lamoda иначе — API отвечает **HTTP 200 практически всегда**, а
результат и ошибка различаются наличием поля `result` или `error`:

    успех:  {"jsonrpc":"2.0","id":"...","result": {...}}
    ошибка: {"jsonrpc":"2.0","id":"...","error": {"code":-32600,"message":"..."}}

Привычная проверка `resp.is_success` такую ошибку пропустит молча. Это ровно тот
класс бага, который в marketplace-mcp дал «молча 0 строк» на WB `goods/filter`,
поэтому распознавание успеха вынесено сюда и покрыто тестом.

Форма запроса (из официальной спеки Lamoda, Swagger 2.0):

    POST https://public-api-seller.lamoda.ru/jsonrpc/v1/orders.list
    {"jsonrpc":"2.0","id":"<uuid4>","method":"v1.orders.list","params":{...}}

Поле `id` в спеке имеет minLength=36 и maxLength=36 — то есть uuid4 это не
стилистический выбор, а требование протокола.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from .errors import classify_rpc_code, make_error

JSONRPC_VERSION = "2.0"
# Спека: id ровно 36 символов (каноническая строка uuid4).
RPC_ID_LENGTH = 36


def new_request_id() -> str:
    """Идентификатор запроса ровно 36 символов, как требует спека."""
    rid = str(uuid.uuid4())
    # Инвариант протокола: строка uuid4 всегда 36 символов. Проверяем явно,
    # чтобы подмена генератора в будущем не сломала контракт молча.
    assert len(rid) == RPC_ID_LENGTH, f"id должен быть {RPC_ID_LENGTH} символов, получено {len(rid)}"
    return rid


def build_envelope(
    method: str,
    params: Optional[dict[str, Any]] = None,
    *,
    request_id: Optional[str] = None,
) -> dict[str, Any]:
    """Собрать тело запроса JSON-RPC.

    `params` всегда присутствует (в спеке поле обязательное): при отсутствии
    аргументов отправляется пустой объект, а не null.
    """
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request_id or new_request_id(),
        "method": method,
        "params": params if params is not None else {},
    }


def is_error_payload(data: Any) -> bool:
    """True, если тело ответа — конверт JSON-RPC с непустой ошибкой.

    Важно: `error: null` в ответе означает УСПЕХ (поле присутствует, но пустое),
    поэтому проверяется не наличие ключа, а истинность значения.
    """
    return isinstance(data, dict) and data.get("error") not in (None, {}, "")


def unwrap(
    data: Any,
    *,
    operation_id: Optional[str] = None,
    endpoint: Optional[str] = None,
    status: int = 200,
) -> dict:
    """Разобрать тело ответа Lamoda в канонический конверт тула.

    Успех  -> {"ok": True, "status": ..., "data": <содержимое result>}
    Ошибка -> канонический конверт ошибки (ok=False).

    `result` отдаётся наружу распакованным: тулы и пагинация работают с полезной
    нагрузкой, а не с обёрткой протокола.
    """
    if not isinstance(data, dict):
        # Не JSON-объект: HTML-заглушка шлюза, текст, пустое тело.
        return make_error(
            "server_error" if status >= 500 else "unknown",
            f"Lamoda вернула не JSON-RPC ответ: {str(data)[:300]}",
            code=status,
            operation_id=operation_id,
            endpoint=endpoint,
            retryable=status >= 500,
        )

    if is_error_payload(data):
        err = data.get("error")
        if not isinstance(err, dict):
            return make_error(
                "rpc_error",
                f"Lamoda вернула ошибку в нераспознанной форме: {str(err)[:300]}",
                operation_id=operation_id,
                endpoint=endpoint,
                details=_capped(data),
            )
        code = err.get("code")
        message = str(err.get("message") or "без описания")
        etype, retryable = classify_rpc_code(
            code if isinstance(code, int) else None, message
        )
        env = make_error(
            etype,
            f"Lamoda JSON-RPC ошибка {code}: {message}",
            code=code if isinstance(code, int) else None,
            operation_id=operation_id,
            endpoint=endpoint,
            retryable=retryable,
            # Полное тело ошибки — чтобы диагноз ставился за один вызов,
            # а не угадыванием (урок marketplace-mcp: читать r['details']).
            details=_capped(data),
        )
        return env

    if "result" not in data:
        # Ни result, ни error — контракт нарушен. Не притворяемся, что это успех:
        # молчаливый успех с пустыми данными и есть тот баг, ради которого
        # написан этот модуль.
        return make_error(
            "rpc_error",
            "Ответ Lamoda не содержит ни 'result', ни 'error' — контракт JSON-RPC "
            f"нарушен: {str(data)[:300]}",
            operation_id=operation_id,
            endpoint=endpoint,
            details=_capped(data),
        )

    return {"ok": True, "status": status, "data": data.get("result")}


def _capped(obj: Any, limit: int = 2000) -> Any:
    """Ограничить размер деталей, чтобы гигантский ответ не залил контекст агента."""
    s = str(obj)
    if len(s) <= limit:
        return obj
    return s[:limit] + f"... [обрезано {len(s) - limit} символов]"
