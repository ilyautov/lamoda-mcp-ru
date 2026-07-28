"""Единый конверт ошибки для всех тулов сервера.

Одна стабильная форма позволяет агенту ветвиться по `error_type` и `retryable`,
не разбирая свободный текст.

Отличие от REST-маркетплейсов (WB/Ozon): у Lamoda транспорт — JSON-RPC 2.0, и
неуспех приезжает с HTTP 200 в поле `error` вида {code, message, data}. Поэтому
здесь ДВЕ функции классификации: `classify_status` для транспортных сбоев и
`classify_rpc_code` для прикладных ошибок внутри конверта.
"""
from __future__ import annotations

from typing import Any, Optional

import httpx

# Канонические типы. Список короткий и стабильный — агенты ветвятся по нему.
ERROR_TYPES = (
    "auth",            # нет/невалидны креды, протух токен
    "forbidden",       # аутентифицирован, но не разрешено (не тот scope)
    "not_found",       # метод или ресурс не существует
    "invalid_params",  # запрос отвергнут как некорректный
    "rate_limit",      # слишком часто — повторить с задержкой
    "conflict",        # конфликт состояния
    "server_error",    # сбой на стороне Lamoda
    "timeout",         # таймаут сети
    "network",         # не удалось соединиться
    "safety_gate",     # заблокировано локально, запрос НЕ отправлялся
    "rpc_error",       # прикладная ошибка JSON-RPC без более точного маппинга
    "unknown",
)

# Стандартные коды JSON-RPC 2.0 (spec.jsonrpc.org). Lamoda использует их же:
# пример в спеке -32600.
RPC_PARSE_ERROR = -32700
RPC_INVALID_REQUEST = -32600
RPC_METHOD_NOT_FOUND = -32601
RPC_INVALID_PARAMS = -32602
RPC_INTERNAL_ERROR = -32603


def make_error(
    error_type: str,
    message: str,
    *,
    code: Optional[int] = None,
    operation_id: Optional[str] = None,
    endpoint: Optional[str] = None,
    retryable: bool = False,
    retry_after_seconds: Optional[float] = None,
    details: Any = None,
) -> dict:
    """Собрать канонический конверт ошибки (тул сам сериализует его в JSON)."""
    if error_type not in ERROR_TYPES:
        error_type = "unknown"
    env: dict[str, Any] = {
        "ok": False,
        "error": error_type,
        "error_type": error_type,
        "message": message,
        "retryable": retryable,
    }
    if code is not None:
        env["code"] = code
    if operation_id:
        env["operation_id"] = operation_id
    if endpoint:
        env["endpoint"] = endpoint
    if retry_after_seconds is not None:
        env["retry_after_seconds"] = retry_after_seconds
    if details is not None:
        env["details"] = details
    return env


def classify_status(status: int) -> tuple[str, bool]:
    """HTTP-статус -> (тип ошибки, повторяемая ли).

    У Lamoda прикладные ошибки приходят с 200, так что сюда попадают в основном
    транспортные сбои и ответы шлюза (502/504) до самого JSON-RPC слоя.
    """
    if status == 401:
        return "auth", False
    if status == 403:
        return "forbidden", False
    if status == 404:
        return "not_found", False
    if status == 409:
        return "conflict", False
    if status == 429:
        return "rate_limit", True
    if 500 <= status < 600:
        return "server_error", True
    if 400 <= status < 500:
        return "invalid_params", False
    return "unknown", False


def classify_rpc_code(code: Optional[int], message: str = "") -> tuple[str, bool]:
    """Код ошибки JSON-RPC -> (тип ошибки, повторяемая ли).

    Стандартные коды (-32700..-32603) заданы спецификацией JSON-RPC и
    отображаются однозначно.

    ДОПУЩЕНИЕ (не проверено живьём): за пределами стандартного диапазона Lamoda
    использует собственные коды, перечня которых в спеке нет — там только пример
    -32600 и свободный `message`. Поэтому для нестандартных кодов мы подстраховкой
    смотрим на текст сообщения, а если и он не опознан — отдаём "rpc_error", а не
    выдумываем более конкретный тип. Когда появятся живые ответы, сюда добавится
    точная таблица кодов.
    """
    standard = {
        RPC_PARSE_ERROR: ("invalid_params", False),
        RPC_INVALID_REQUEST: ("invalid_params", False),
        RPC_METHOD_NOT_FOUND: ("not_found", False),
        RPC_INVALID_PARAMS: ("invalid_params", False),
        RPC_INTERNAL_ERROR: ("server_error", True),
    }
    if code in standard:
        return standard[code]

    low = (message or "").lower()
    # Подсказки по тексту — только для явно узнаваемых случаев.
    if any(w in low for w in ("unauthor", "token", "не авторизов", "токен")):
        return "auth", False
    if any(w in low for w in ("forbidden", "permission", "denied", "доступ", "запрещ")):
        return "forbidden", False
    if any(w in low for w in ("not found", "не найден", "отсутств")):
        return "not_found", False
    if any(w in low for w in ("rate limit", "too many", "лимит", "слишком часто")):
        return "rate_limit", True
    if any(w in low for w in ("invalid", "valid", "некоррект", "невер")):
        return "invalid_params", False
    # Диапазон -32000..-32099 спецификация отводит под серверные ошибки реализации.
    if code is not None and -32099 <= code <= -32000:
        return "server_error", True
    return "rpc_error", False


def error_from_exception(
    exc: Exception,
    *,
    operation_id: Optional[str] = None,
    endpoint: Optional[str] = None,
) -> dict:
    """Транспортное исключение -> канонический конверт."""
    if isinstance(exc, httpx.TimeoutException):
        return make_error(
            "timeout",
            "Истёк таймаут запроса — Lamoda не ответила вовремя. Повторите позже.",
            operation_id=operation_id,
            endpoint=endpoint,
            retryable=True,
        )
    if isinstance(exc, httpx.ConnectError):
        return make_error(
            "network",
            "Не удалось соединиться с хостом Lamoda. Проверьте сеть и доступ из "
            "вашего региона.",
            operation_id=operation_id,
            endpoint=endpoint,
            retryable=True,
        )
    return make_error(
        "unknown",
        f"Непредвиденная ошибка: {type(exc).__name__}: {exc}",
        operation_id=operation_id,
        endpoint=endpoint,
    )
