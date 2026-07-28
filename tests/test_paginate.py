"""Постраничный обход и защита от «молча ноль строк»."""
from __future__ import annotations

import httpx
import pytest

from core.paginate import fetch_all
from core.registry import EndpointSpec
from tests.test_client import TOKEN, make_client, rpc_ok


def spec(items_path="result.items", pagination="page") -> EndpointSpec:
    return EndpointSpec(
        operation_id="v1.orders.list",
        path="/jsonrpc/v1/orders.list",
        safety="read",
        pagination=pagination,
        items_path=items_path,
    )


def token_or(fn):
    def handler(request: httpx.Request) -> httpx.Response:
        if "tokens" in str(request.url):
            return rpc_ok(TOKEN)
        return fn(request)
    return handler


@pytest.mark.asyncio
async def test_обход_собирает_все_страницы():
    pages = {
        1: [{"id": 1}, {"id": 2}],
        2: [{"id": 3}],
        3: [],
    }

    def data(request: httpx.Request) -> httpx.Response:
        import json
        page = json.loads(request.content)["params"]["page"]
        return rpc_ok({"items": pages.get(page, [])})

    client = make_client(token_or(data))
    out = await fetch_all(client, spec(), limit=2)

    assert out["ok"] is True
    assert out["total_fetched"] == 3
    assert out["pages_fetched"] == 3


@pytest.mark.asyncio
async def test_остановка_по_общему_количеству():
    def data(request: httpx.Request) -> httpx.Response:
        return rpc_ok({"items": [{"id": 1}, {"id": 2}], "total": 2})

    client = make_client(token_or(data))
    out = await fetch_all(client, spec(), limit=2)

    # Вторая страница не запрашивается: всё уже собрано.
    assert out["pages_fetched"] == 1
    assert out["total_fetched"] == 2


@pytest.mark.asyncio
async def test_расхождение_items_path_со_спекой_видно_в_ответе():
    """Урок marketplace-mcp: схема обещала result.items, живой ответ дал другое."""
    def data(request: httpx.Request) -> httpx.Response:
        # Каталог объявляет result.items, а Lamoda отдаёт result.list.
        return rpc_ok({"list": [{"id": 1}], "total": 1})

    client = make_client(token_or(data))
    out = await fetch_all(client, spec(items_path="result.items"), limit=10)

    assert out["total_fetched"] == 1
    assert "warning" in out
    assert "расходится со спекой" in out["warning"]


@pytest.mark.asyncio
async def test_ноль_строк_сопровождается_объяснением():
    """«Молча ноль» должно отличаться от «данных нет»."""
    def data(request: httpx.Request) -> httpx.Response:
        return rpc_ok({"somethingElse": 42})

    client = make_client(token_or(data))
    out = await fetch_all(client, spec(items_path="result.items"), limit=10)

    assert out["total_fetched"] == 0
    assert "note" in out
    assert "result.items" in out["note"]
    assert "somethingElse" in out["note"]


@pytest.mark.asyncio
async def test_ограничение_по_числу_строк():
    def data(request: httpx.Request) -> httpx.Response:
        return rpc_ok({"items": [{"id": i} for i in range(50)]})

    client = make_client(token_or(data))
    out = await fetch_all(client, spec(), limit=50, max_items=120)

    assert out["truncated"] is True
    assert out["total_fetched"] == 120


@pytest.mark.asyncio
async def test_ошибка_страницы_прерывает_обход():
    from tests.test_client import rpc_err

    def data(request: httpx.Request) -> httpx.Response:
        return rpc_err(-32602, "bad params")

    client = make_client(token_or(data))
    out = await fetch_all(client, spec(), limit=10)

    assert out["ok"] is False
    assert out["error_type"] == "invalid_params"


@pytest.mark.asyncio
async def test_непагинируемый_метод_делает_один_запрос():
    calls = {"n": 0}

    def data(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return rpc_ok({"items": [{"id": 1}]})

    client = make_client(token_or(data))
    out = await fetch_all(client, spec(pagination="none"), limit=10)

    assert calls["n"] == 1
    assert out["total_fetched"] == 1
