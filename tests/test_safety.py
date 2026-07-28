"""Гейт безопасности: подтверждения и предупреждение о непроверенных методах."""
from __future__ import annotations

from core.safety import check_gate, normalize, stricter


def test_чтение_проходит_без_подтверждений():
    assert check_gate("read", confirm_write=False,
                      i_understand_this_modifies_data=False) is None


def test_запись_без_подтверждения_блокируется():
    err = check_gate("write", confirm_write=False,
                     i_understand_this_modifies_data=False,
                     operation_id="v1.nomenclatures.update-price")
    assert err is not None
    assert err["error_type"] == "safety_gate"
    assert err["details"]["http_call_skipped"] is True


def test_запись_с_подтверждением_проходит():
    assert check_gate("write", confirm_write=True,
                      i_understand_this_modifies_data=False) is None


def test_разрушающей_операции_мало_одного_подтверждения():
    err = check_gate("destructive", confirm_write=True,
                     i_understand_this_modifies_data=False,
                     operation_id="v1.products.delete")
    assert err is not None
    assert "i_understand_this_modifies_data=true" in err["details"]["required"]


def test_разрушающая_с_обоими_подтверждениями_проходит():
    assert check_gate("destructive", confirm_write=True,
                      i_understand_this_modifies_data=True) is None


def test_непроверенный_метод_предупреждает_об_этом():
    err = check_gate("write", confirm_write=False,
                     i_understand_this_modifies_data=False,
                     operation_id="v1.nomenclatures.update-stock",
                     live_verified=False)
    assert "ни разу не выполнялся на живом кабинете" in err["message"]
    assert "dry_run" in err["message"]


def test_проверенный_метод_не_получает_лишнего_предупреждения():
    err = check_gate("write", confirm_write=False,
                     i_understand_this_modifies_data=False,
                     operation_id="v1.some.method",
                     live_verified=True)
    assert "ни разу не выполнялся" not in err["message"]


def test_неизвестный_уровень_трактуется_как_запись():
    """Неопределённость обязана ужесточать гейт, а не ослаблять его."""
    assert normalize("непонятно") == "write"
    assert normalize(None) == "write"
    assert check_gate("непонятно", confirm_write=False,
                      i_understand_this_modifies_data=False) is not None


def test_выбор_более_строгого_уровня():
    assert stricter("read", "write") == "write"
    assert stricter("write", "destructive") == "destructive"
    assert stricter("read", "read") == "read"
