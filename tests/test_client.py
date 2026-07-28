"""Клиент: авторизация, кэш токена, авто-рефреш, dry_run, allowlist хостов.

Все запросы идут через httpx.MockTransport — сеть не используется.
"""
from __future__ import annotations

import json

import httpx
import pytest

from core.client import LamodaClient, ServiceConfig
from core.credentials import CredentialStore

CREDS = {"client_id": "id-123", "client_secret": "secret-456"}


class FakeStore(CredentialStore):
    """Хранилище, отдающее фиксированные креды, не трогая диск."""

    def __init__(self, creds=None):
        self._creds = creds if creds is not None else dict(CREDS)

    def resolve(self, fields=None):
        return dict(self._creds), "test"

    def missing(self, fields=None):
        return [f for f in (fields or ["client_id", "client_secret"])
                if not self._creds.get(f)]


def make_client(handler, creds=None) -> LamodaClient:
    cfg = ServiceConfig(store=FakeStore(creds))
    return LamodaClient(cfg, transport=httpx.MockTransport(handler))


def rpc_ok(result) -> httpx.Response:
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": "x" * 36, "result": result},
        headers={"Content-Type": "application/json"},
    )


def rpc_err(code: int, message: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"jsonrpc": "2.0", "id": "x" * 36,
              "error": {"code": code, "message": message}},
        headers={"Content-Type": "application/json"},
    )


TOKEN = {"accessToken": "tok-1", "expiresIn": 900, "refreshToken": "ref-1",
         "scope": "r_orders r_products", "tokenType": "bearer"}


@pytest.mark.asyncio
async def test_путь_метода_строится_из_имени():
    client = make_client(lambda r: rpc_ok({}))
    assert client.method_path("v1.orders.list") == "/jsonrpc/v1/orders.list"
    assert client.method_path("v2.stock.list") == "/jsonrpc/v2/stock.list"


@pytest.mark.asyncio
async def test_токен_запрашивается_и_подставляется_в_заголовок():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "tokens.create" in str(request.url):
            body = json.loads(request.content)
            # Имена полей в спеке — camelCase.
            assert body["params"]["clientId"] == "id-123"
            assert body["params"]["grantType"] == "client_credentials"
            return rpc_ok(TOKEN)
        return rpc_ok({"items": []})

    client = make_client(handler)
    out = await client.call("v1.orders.list", {"limit": 5})

    assert out["ok"] is True
    assert seen[-1].headers["Authorization"] == "Bearer tok-1"


@pytest.mark.asyncio
async def test_токен_берётся_из_кэша_на_втором_вызове():
    calls = {"token": 0, "data": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "tokens.create" in str(request.url):
            calls["token"] += 1
            return rpc_ok(TOKEN)
        calls["data"] += 1
        return rpc_ok({"items": []})

    client = make_client(handler)
    await client.call("v1.orders.list")
    await client.call("v1.brands.list")

    assert calls["data"] == 2
    assert calls["token"] == 1, "токен должен запрашиваться один раз"


@pytest.mark.asyncio
async def test_протухший_токен_обновляется_и_запрос_повторяется():
    state = {"token_calls": 0, "data_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "tokens.create" in url:
            state["token_calls"] += 1
            return rpc_ok({**TOKEN, "accessToken": f"tok-{state['token_calls']}"})
        if "tokens.update" in url:
            state["token_calls"] += 1
            return rpc_err(4010, "refresh token expired")
        state["data_calls"] += 1
        if state["data_calls"] == 1:
            # Lamoda сообщает о протухшем токене с HTTP 200.
            return rpc_err(4010, "Unauthorized: token expired")
        return rpc_ok({"items": [1]})

    client = make_client(handler)
    out = await client.call("v1.orders.list")

    assert out["ok"] is True
    assert state["data_calls"] == 2, "запрос должен быть повторён после обновления"
    assert state["token_calls"] >= 2


@pytest.mark.asyncio
async def test_повторное_обновление_не_зацикливается():
    """Если токен протух и после обновления — отдаём ошибку, а не крутим цикл."""
    state = {"data_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "tokens" in str(request.url):
            return rpc_ok(TOKEN)
        state["data_calls"] += 1
        return rpc_err(4010, "Unauthorized")

    client = make_client(handler)
    out = await client.call("v1.orders.list")

    assert out["ok"] is False
    assert out["error_type"] == "auth"
    assert state["data_calls"] == 2, "ровно одна попытка обновления"


@pytest.mark.asyncio
async def test_dry_run_ничего_не_отправляет():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("при dry_run не должно быть ни одного запроса")

    client = make_client(handler)
    out = await client.call("v1.nomenclatures.update-price",
                            {"sku": "A1", "price": 100}, dry_run=True)

    assert out["dry_run"] is True
    assert out["http_call_skipped"] is True
    body = out["request"]["body"]
    assert body["method"] == "v1.nomenclatures.update-price"
    assert body["params"] == {"sku": "A1", "price": 100}
    assert body["jsonrpc"] == "2.0"


@pytest.mark.asyncio
async def test_нет_кредов_сразу_ошибка_авторизации():
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("без кредов запрос отправлять нельзя")

    client = make_client(handler, creds={"client_id": "", "client_secret": ""})
    out = await client.call("v1.orders.list")

    assert out["ok"] is False
    assert out["error_type"] == "auth"


@pytest.mark.asyncio
async def test_чужой_хост_отклоняется_до_отправки():
    """Защита от увода кредов через промпт-инъекцию."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("на чужой хост запрос уходить не должен")

    cfg = ServiceConfig(store=FakeStore(), host="public-api-seller.lamoda.ru.evil.com")
    client = LamodaClient(cfg, transport=httpx.MockTransport(handler))
    out = await client.call("v1.orders.list")

    assert out["ok"] is False
    assert out["error_type"] == "forbidden"


def test_allowlist_привязан_к_границе_точки():
    cfg = ServiceConfig(store=FakeStore())
    assert cfg.host_allowed("public-api-seller.lamoda.ru") is True
    assert cfg.host_allowed("seller-gateway.service.lamoda.tech") is True
    assert cfg.host_allowed("lamoda.ru.evil.com") is False
    assert cfg.host_allowed("evil-lamoda.ru") is False


@pytest.mark.asyncio
async def test_вызов_по_каталогу_использует_путь_из_каталога():
    """Регресс: путь с вложенным сегментом нельзя выводить из имени метода.

    У '/v1/fbo/warehouse.list' идентификатор 'v1.fbo.warehouse.list'.
    Восстановление пути из имени дало бы '/jsonrpc/v1/fbo.warehouse.list' —
    другой адрес, и запрос ушёл бы не туда.
    """
    from core.registry import EndpointSpec

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if "tokens" in str(request.url):
            return rpc_ok(TOKEN)
        return rpc_ok({"items": []})

    spec = EndpointSpec(
        operation_id="v1.fbo.warehouse.list",
        path="/jsonrpc/v1/fbo/warehouse.list",
        safety="read",
    )
    client = make_client(handler)
    await client.call_spec(spec, {})

    assert seen[-1] == "/jsonrpc/v1/fbo/warehouse.list"


@pytest.mark.asyncio
async def test_ошибка_токена_прокидывается_а_не_маскируется():
    def handler(request: httpx.Request) -> httpx.Response:
        if "tokens.create" in str(request.url):
            return rpc_err(-32602, "invalid client credentials")
        raise AssertionError("до данных дойти не должно")

    client = make_client(handler)
    out = await client.call("v1.orders.list")

    assert out["ok"] is False
    assert "invalid client credentials" in out["message"]
