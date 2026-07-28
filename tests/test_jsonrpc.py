"""Конверт JSON-RPC: сборка запроса и разбор ответа.

Главное, что здесь проверяется, — ошибка Lamoda приезжает с HTTP 200 и НЕ
должна выглядеть успехом. Это тот самый класс бага, который в marketplace-mcp
дал «молча ноль строк».
"""
from __future__ import annotations

from core.jsonrpc import (
    JSONRPC_VERSION,
    RPC_ID_LENGTH,
    build_envelope,
    is_error_payload,
    new_request_id,
    unwrap,
)


def test_id_ровно_36_символов():
    # Спека Lamoda задаёт minLength=36 и maxLength=36 — это требование, а не стиль.
    for _ in range(20):
        assert len(new_request_id()) == RPC_ID_LENGTH


def test_конверт_содержит_обязательные_поля():
    env = build_envelope("v1.orders.list", {"limit": 10})
    assert env["jsonrpc"] == JSONRPC_VERSION
    assert env["method"] == "v1.orders.list"
    assert env["params"] == {"limit": 10}
    assert len(env["id"]) == RPC_ID_LENGTH


def test_params_пустой_объект_а_не_null():
    # В спеке params обязательное поле; null сломал бы валидацию на сервере.
    env = build_envelope("v1.brands.list")
    assert env["params"] == {}


def test_успешный_ответ_распаковывается_до_result():
    body = {"jsonrpc": "2.0", "id": "x" * 36, "result": {"items": [1, 2, 3]}}
    out = unwrap(body)
    assert out["ok"] is True
    assert out["data"] == {"items": [1, 2, 3]}


def test_ошибка_при_http_200_не_считается_успехом():
    # Ключевой случай Lamoda: транспорт вернул 200, но операция провалилась.
    body = {
        "jsonrpc": "2.0",
        "id": "x" * 36,
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    out = unwrap(body, status=200)
    assert out["ok"] is False
    assert out["error_type"] == "invalid_params"
    assert out["code"] == -32600
    # Полное тело ошибки должно быть в деталях: диагноз ставится за один вызов.
    assert out["details"]["error"]["message"] == "Invalid Request"


def test_error_null_означает_успех():
    # Поле присутствует, но пустое — это НЕ ошибка.
    body = {"jsonrpc": "2.0", "id": "x" * 36, "error": None, "result": {"ok": 1}}
    assert is_error_payload(body) is False
    assert unwrap(body)["ok"] is True


def test_метод_не_найден_отображается_в_not_found():
    body = {"jsonrpc": "2.0", "id": "x" * 36,
            "error": {"code": -32601, "message": "Method not found"}}
    assert unwrap(body)["error_type"] == "not_found"


def test_ошибка_авторизации_распознаётся_по_тексту():
    # Нестандартный код: единственная зацепка — текст сообщения.
    body = {"jsonrpc": "2.0", "id": "x" * 36,
            "error": {"code": 4010, "message": "Unauthorized: token expired"}}
    out = unwrap(body)
    assert out["error_type"] == "auth"
    assert out["retryable"] is False


def test_ответ_без_result_и_без_error_это_ошибка():
    # Молчаливый «успех» с пустыми данными — ровно то, что нужно предотвратить.
    body = {"jsonrpc": "2.0", "id": "x" * 36}
    out = unwrap(body)
    assert out["ok"] is False
    assert out["error_type"] == "rpc_error"


def test_не_json_ответ_не_ломает_разбор():
    out = unwrap("<html>502 Bad Gateway</html>", status=502)
    assert out["ok"] is False
    assert out["retryable"] is True
