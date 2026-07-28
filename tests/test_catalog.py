"""Каталог: целостность, инвариант безопасности, живость ссылок в рецептах.

Ключевой тест здесь — «ноль мутаций в read». У Lamoda все методы POST, поэтому
подстраховки по HTTP-глаголу нет: если сюда просочится неверная разметка,
ничто другое её не поймает.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from core.registry import Catalog

ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = ROOT / "lamoda_mcp" / "endpoints.yaml"
WORKFLOWS_PATH = ROOT / "lamoda_mcp" / "workflows.yaml"

catalog = Catalog.from_yaml(CATALOG_PATH)

# Глаголы-мутации; проверяются как отдельные сегменты имени, а не подстрокой —
# подстрочный поиск давал ложные срабатывания ('count' внутри 'disCount').
MUTATION_VERBS = {
    "create", "update", "delete", "remove", "cancel", "set", "add", "store",
    "approve", "upload", "import", "apply", "send", "close", "reset", "hide",
    "activate", "confirm", "generate",
}
READ_TAILS = {
    "status", "statuses", "info", "history", "check", "list", "get", "total",
    "report", "summary", "count", "count-by-statuses", "status-history",
    "template", "added", "finished", "download", "calculate",
}


def segments(name: str) -> set[str]:
    out: set[str] = set()
    for dotted in name.split("."):
        out.update(p for p in dotted.split("-") if p)
    return out


def test_каталог_загружается_и_непустой():
    assert len(catalog.all()) == 155


def test_идентификаторы_уникальны():
    ids = [s.operation_id for s in catalog.all()]
    assert len(ids) == len(set(ids))


def test_ноль_мутаций_помеченных_чтением():
    """Главный инвариант безопасности каталога."""
    offenders = []
    for spec in catalog.all():
        if spec.safety != "read":
            continue
        tail = spec.operation_id.split(".")[-1]
        if tail in READ_TAILS:
            continue
        hit = segments(spec.operation_id) & MUTATION_VERBS
        if hit:
            offenders.append(f"{spec.operation_id} (глагол: {', '.join(sorted(hit))})")
    assert not offenders, "мутации помечены как чтение: " + "; ".join(offenders)


def test_имя_метода_в_теле_тоже_не_выглядит_мутацией_у_read():
    """Случай validate-price -> set-price: путь читается как чтение, а тело — нет."""
    offenders = []
    for spec in catalog.all():
        if spec.safety != "read" or spec.rpc_method == spec.operation_id:
            continue
        tail = spec.rpc_method.split(".")[-1]
        if tail in READ_TAILS:
            continue
        hit = segments(spec.rpc_method) & MUTATION_VERBS
        if hit:
            offenders.append(f"{spec.operation_id}: method='{spec.rpc_method}'")
    assert not offenders, "read-метод отправляет мутирующее имя: " + "; ".join(offenders)


def test_уровни_безопасности_допустимые():
    for spec in catalog.all():
        assert spec.safety in ("read", "write", "destructive"), spec.operation_id


def test_все_методы_помечены_непроверенными():
    """Каталог собран офлайн. Пока не прошёл живой прогон, честность важнее вида."""
    assert all(s.live_verified is False for s in catalog.all())


def test_путь_согласован_с_идентификатором():
    """operation_id получается из пути заменой '/' на '.' — и только так.

    Обратное преобразование неоднозначно: у '/v1/fbo/warehouse.list' и
    гипотетического '/v1/fbo.warehouse.list' один и тот же идентификатор
    'v1.fbo.warehouse.list'. Поэтому путь хранится в каталоге, а не выводится
    в момент вызова — этот тест и поймал соответствующий баг в клиенте.
    """
    for spec in catalog.all():
        assert spec.path.startswith("/jsonrpc/"), spec.operation_id
        tail = spec.path[len("/jsonrpc/"):]
        assert tail.replace("/", ".") == spec.operation_id, (
            f"{spec.operation_id}: путь {spec.path} даёт "
            f"{tail.replace('/', '.')}"
        )


def test_вложенные_пути_не_выводятся_из_имени():
    """Явно фиксируем случай, ради которого путь хранится отдельным полем."""
    spec = catalog.get("v1.fbo.warehouse.list")
    assert spec is not None
    assert spec.path == "/jsonrpc/v1/fbo/warehouse.list"


def test_опасные_ценовые_методы_не_читающие():
    """Ценовые операции без явного описания-геттера должны быть записью."""
    for oid in ("v1.nomenclature.validate-price",
                "v1.nomenclature.auto-conversion-price.calculate",
                "v1.nomenclature.set-price",
                "v1.nomenclatures.update-stock",
                "v1.nomenclatures.set-prices"):
        spec = catalog.get(oid)
        assert spec is not None, f"{oid} пропал из каталога"
        assert spec.safety in ("write", "destructive"), f"{oid} помечен {spec.safety}"


def test_разрушающие_методы_на_месте():
    for oid in ("v1.products.delete", "v1.order.reset"):
        spec = catalog.get(oid)
        assert spec is not None and spec.safety == "destructive", oid


def test_служебные_методы_скрыты():
    for oid in ("v1.tokens.create", "v1.tokens.update", "v1.order.reset"):
        spec = catalog.get(oid)
        assert spec is not None and spec.internal is True, oid


def test_поиск_на_русском_находит_методы():
    assert any("order" in s.operation_id for s in catalog.search("заказы"))
    assert any("stock" in s.operation_id for s in catalog.search("остатки"))
    assert any("price" in s.operation_id or "prices" in s.operation_id
               for s in catalog.search("цены"))


def test_поиск_не_показывает_служебное_по_умолчанию():
    found = catalog.search("токен")
    assert all(not s.internal for s in found)


def test_пробел_в_items_path_зафиксирован_а_не_скрыт():
    """Схема раскрывает путь к строкам не у всех постраничных методов.

    Обход по ним всё равно работает: walker перебирает запасные пути и
    сообщает, какой сработал. Но пробел должен быть записан в каталоге —
    непроверенное место, о котором не знают, и превращается потом в «ноль
    строк без объяснения».
    """
    actual = sorted(s.operation_id for s in catalog.all()
                    if s.pagination == "page" and not s.items_path)
    assert actual == sorted(catalog.items_path_unresolved), (
        "список нерешённых items_path в каталоге разошёлся с фактическим — "
        "пересоберите каталог: python3 scripts/ingest_lamoda.py"
    )


def test_у_большинства_постраничных_путь_к_строкам_известен():
    paged = [s for s in catalog.all() if s.pagination == "page"]
    resolved = [s for s in paged if s.items_path]
    assert len(paged) > 0
    # Порог намеренно мягкий: это защита от обвала выведения при правке
    # ingest-скрипта, а не утверждение о качестве спеки Lamoda.
    assert len(resolved) / len(paged) >= 0.4, (
        f"путь к строкам известен лишь у {len(resolved)} из {len(paged)}"
    )


def test_ссылки_в_рецептах_живые():
    doc = yaml.safe_load(WORKFLOWS_PATH.read_text(encoding="utf-8"))
    problems = []
    for name, flow in (doc.get("workflows") or {}).items():
        for step in flow.get("steps", []):
            oid = step.get("method")
            spec = catalog.get(oid)
            if spec is None:
                problems.append(f"{name}: метода '{oid}' нет в каталоге")
            elif spec.safety != "read":
                problems.append(f"{name}: '{oid}' не чтение ({spec.safety})")
            elif spec.internal:
                problems.append(f"{name}: '{oid}' служебный")
    assert not problems, "; ".join(problems)
