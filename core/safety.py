"""Гейт для пишущих и разрушающих операций.

Ключ от кабинета Lamoda даёт власть над ценами, остатками и заказами. Ошибочная
запись роняет цену втрое или обнуляет сток. Мутации гасятся локально — до того,
как запрос покинет машину.

Уровни:
- read        — подтверждение не нужно
- write       — нужен confirm_write=True
- destructive — нужен confirm_write=True И i_understand_this_modifies_data=True

ВАЖНОЕ ОТЛИЧИЕ ОТ WB/OZON. Там подстраховкой служил «пол» по HTTP-глаголу:
метод с PUT/PATCH/DELETE не мог быть помечен как read, даже если импортёр спеки
ошибся. У Lamoda **все 155 методов — POST** (JSON-RPC), поэтому независимого
сигнала о мутации не существует вовсе. Единственная защита — ручная курация
`safety_overrides.yaml` плюс тест-инвариант «ноль мутаций в read».
Автоклассификация по имени метода здесь только черновик, ей нельзя доверять:
в UNKNOWN попадают ровно денежные операции (set-price, update-stock,
prices-approve, order.reset).

Вторая особенность v1: каталог собран офлайн, ни один запрос не уходил в живой
кабинет. Поэтому в предупреждении гейта печатается статус проверки метода —
пользователь должен видеть, что подтверждает вызов, форма которого выведена из
схемы, а не подтверждена боем.
"""
from __future__ import annotations

from typing import Optional

from .errors import make_error

SAFETY_LEVELS = ("read", "write", "destructive")
_RANK = {"read": 0, "write": 1, "destructive": 2}


def normalize(declared: Optional[str]) -> str:
    """Привести уровень к каноническому.

    Неизвестное значение трактуется как `write`, а не `read`: неопределённость
    должна ужесточать гейт, а не ослаблять его.
    """
    if declared in SAFETY_LEVELS:
        return declared  # type: ignore[return-value]
    return "write"


def stricter(a: str, b: str) -> str:
    """Более строгий из двух уровней."""
    return a if _RANK[normalize(a)] >= _RANK[normalize(b)] else b


def _verification_note(live_verified: bool) -> str:
    if live_verified:
        return ""
    return (
        " ВНИМАНИЕ: этот метод ни разу не выполнялся на живом кабинете Lamoda — "
        "его параметры выведены из официальной спеки, но боем не подтверждены. "
        "Сначала имеет смысл вызвать его с dry_run=true и проверить тело запроса."
    )


def check_gate(
    safety: str,
    *,
    confirm_write: bool,
    i_understand_this_modifies_data: bool,
    operation_id: Optional[str] = None,
    live_verified: bool = False,
) -> Optional[dict]:
    """Вернуть конверт ошибки, если гейт НЕ пройден, иначе None.

    Возврат означает, что не отправлено ничего (`http_call_skipped`).
    """
    level = normalize(safety)
    if level == "read":
        return None

    note = _verification_note(live_verified)

    if level == "write" and not confirm_write:
        return make_error(
            "safety_gate",
            f"Это операция ЗАПИСИ ({operation_id}). Она изменит данные в кабинете "
            f"Lamoda. Передайте confirm_write=true, чтобы выполнить. "
            f"Сейчас не отправлено ничего.{note}",
            operation_id=operation_id,
            retryable=False,
            details={
                "required": ["confirm_write=true"],
                "http_call_skipped": True,
                "live_verified": live_verified,
            },
        )

    if level == "destructive":
        missing = []
        if not confirm_write:
            missing.append("confirm_write=true")
        if not i_understand_this_modifies_data:
            missing.append("i_understand_this_modifies_data=true")
        if missing:
            return make_error(
                "safety_gate",
                f"Это РАЗРУШАЮЩАЯ операция ({operation_id}): она удаляет или "
                f"необратимо меняет данные. Нужны оба подтверждения. "
                f"Сейчас не отправлено ничего.{note}",
                operation_id=operation_id,
                retryable=False,
                details={
                    "required": missing,
                    "http_call_skipped": True,
                    "live_verified": live_verified,
                },
            )
    return None
