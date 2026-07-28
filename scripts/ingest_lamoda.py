#!/usr/bin/env python3
"""Собрать `lamoda_mcp/endpoints.yaml` из официальной спеки Lamoda.

Источник: https://public-api-seller.lamoda.ru/swagger.json (Swagger 2.0, открыт
без авторизации). Локальная копия лежит рядом — `scripts/lamoda_swagger.json`,
чтобы сборка была воспроизводимой и не зависела от сети.

Что делает скрипт:
  1. читает пути спеки (155 операций, все POST — это JSON-RPC);
  2. раскладывает 86 сырых префиксов имён по бизнес-секциям;
  3. выводит `items_path` из схемы ответа и стиль пагинации из схемы параметров;
  4. проставляет ЧЕРНОВИК safety по имени метода;
  5. НАКЛАДЫВАЕТ поверх ручную курацию из `lamoda_mcp/safety_overrides.yaml`
     — она и есть источник истины.

Про пункты 4 и 5. Автоклассификация по имени оставляет 44 метода
неопознанными, и именно среди них лежат денежные операции (set-price,
update-stock, prices-approve, order.reset). У Lamoda все методы POST, поэтому
подстраховки «по HTTP-глаголу», которая работала в marketplace-mcp, здесь нет
вовсе. Любой метод, не покрытый ручной курацией и не опознанный уверенно,
получает `write` — неопределённость обязана ужесточать гейт, а не ослаблять.

Запуск:
    python3 scripts/ingest_lamoda.py                 # из локальной копии
    python3 scripts/ingest_lamoda.py --fetch         # скачать спеку заново
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC_LOCAL = ROOT / "scripts" / "lamoda_swagger.json"
SPEC_URL = "https://public-api-seller.lamoda.ru/swagger.json"
OUT_PATH = ROOT / "lamoda_mcp" / "endpoints.yaml"
OVERRIDES_PATH = ROOT / "lamoda_mcp" / "safety_overrides.yaml"

PUBLIC_HOST = "public-api-seller.lamoda.ru"
BASE_PATH = "/jsonrpc"

# --- секции --------------------------------------------------------------
# Сырые префиксы имён методов -> бизнес-области. Порядок важен: правила
# проверяются сверху вниз, первое совпадение по подстроке выигрывает.
SECTION_RULES: list[tuple[str, str]] = [
    ("matching-manager/price-index", "prices"),
    ("matching-manager/rfc", "prices"),
    ("matching-manager", "prices"),
    ("competitor-price", "prices"),
    ("product-price-statuses", "prices"),
    ("nomenclature-images", "content"),
    ("nomenclature", "catalog"),
    ("attributes-dictionaries", "catalog"),
    ("attribute-dictionaries", "catalog"),
    ("attributes", "catalog"),
    ("erp-categories", "catalog"),
    ("categor", "catalog"),
    ("brands", "catalog"),
    ("dictionaries", "catalog"),
    ("recommended-products", "products"),
    ("recommended-metrics", "products"),
    ("fraud-products-report", "products"),
    ("products-list-export", "products"),
    ("products-added", "products"),
    ("products-add", "products"),
    ("product", "products"),
    ("offer", "offers"),
    ("shipments-fulfilment", "shipments"),
    ("shipment", "shipments"),
    ("supplier-fulfilment-returns", "returns"),
    ("supplier-return", "returns"),
    ("supplier_return", "returns"),
    ("return-boxes", "returns"),
    ("anomaly-box", "returns"),
    ("box", "returns"),
    ("order", "orders"),
    ("stock-illiquid", "stocks"),
    ("stocks-illiquid", "stocks"),
    ("stock", "stocks"),
    ("warehouse", "warehouses"),
    ("fbo", "warehouses"),
    ("fbs", "orders"),
    ("promotion", "promotions"),
    ("promo", "promotions"),
    ("label-", "labels"),
    ("pack", "labels"),
    ("file", "files"),
    ("image", "files"),
    ("import", "files"),
    ("export", "files"),
    ("async-api-request", "files"),
    ("statistic", "statistics"),
    ("total", "statistics"),
    ("summary", "statistics"),
    ("token", "account"),
    ("partner", "account"),
    ("seller-profile", "account"),
    ("seller-brands", "account"),
    ("personal", "account"),
    ("reseller", "account"),
    ("settings", "account"),
    ("user", "account"),
    ("validation", "validation"),
    ("item", "orders"),
]

SECTION_TITLES = {
    "products": "Товары",
    "catalog": "Номенклатура и справочники",
    "prices": "Цены и ценовой индекс",
    "orders": "Заказы",
    "shipments": "Отгрузки",
    "stocks": "Остатки",
    "warehouses": "Склады",
    "promotions": "Акции",
    "returns": "Возвраты",
    "labels": "Ярлыки и упаковка",
    "files": "Файлы, импорт и экспорт",
    "statistics": "Статистика",
    "offers": "Офферы",
    "account": "Аккаунт и доступ",
    "validation": "Валидация",
    "general": "Прочее",
}

# --- черновая классификация safety --------------------------------------
# ТОЛЬКО черновик. Источник истины — safety_overrides.yaml.
READ_SUFFIXES = {
    "list", "get", "check", "info", "history", "calculate", "count", "statuses",
    "search", "download", "total", "summary", "report", "template", "added",
    "finished", "get-id", "count-by-statuses", "get-brands",
    "get-axapta-categories", "price-threshold-exceeded",
}
WRITE_SUFFIXES = {
    "create", "update", "upload", "set", "import", "apply", "send", "close",
    "print", "generate", "store", "add", "approve", "reset", "activate_in_ui",
    "hide-moderation", "set-price", "set-price-promo", "update-price",
    "validate-price", "prices-approve", "set-prices", "update-activation",
    "update-stock", "set-recommended-prices", "update-events", "update-event",
    "first-login", "pack",
}
DESTRUCTIVE_SUFFIXES = {"delete", "remove", "cancel"}

# Порядок строгости — чтобы при конфликте источников брать более строгий уровень.
SAFETY_RANK = {"read": 0, "unknown": 1, "write": 2, "destructive": 3}

# Русские ключевые слова по секциям — чтобы русский запрос находил метод
# с английским именем.
SECTION_KEYWORDS = {
    "products": ["товары", "товар", "карточки", "ассортимент"],
    "catalog": ["номенклатура", "справочник", "атрибуты", "категории", "бренды"],
    "prices": ["цены", "цена", "прайс", "ценовой индекс", "конкуренты", "скидка"],
    "orders": ["заказы", "заказ", "покупки"],
    "shipments": ["отгрузки", "отгрузка", "поставка", "доставка"],
    "stocks": ["остатки", "сток", "склад", "неликвид"],
    "warehouses": ["склады", "склад", "фбо"],
    "promotions": ["акции", "акция", "промо", "скидки"],
    "returns": ["возвраты", "возврат", "короба"],
    "labels": ["ярлыки", "этикетки", "упаковка", "маркировка"],
    "files": ["файлы", "импорт", "экспорт", "выгрузка", "загрузка", "изображения"],
    "statistics": ["статистика", "метрики", "отчёт", "итого"],
    "offers": ["офферы", "оффер", "предложения"],
    "account": ["аккаунт", "доступ", "токен", "профиль", "партнёр", "настройки"],
    "validation": ["валидация", "проверка", "ошибки"],
    "general": [],
}


def load_spec(fetch: bool) -> dict:
    if fetch:
        import urllib.request

        print(f"Скачиваю {SPEC_URL} ...", file=sys.stderr)
        with urllib.request.urlopen(SPEC_URL, timeout=60) as fh:  # noqa: S310
            raw = fh.read()
        SPEC_LOCAL.write_bytes(raw)
        print(f"Сохранено в {SPEC_LOCAL} ({len(raw)} байт)", file=sys.stderr)
    if not SPEC_LOCAL.exists():
        sys.exit(f"Нет файла спеки {SPEC_LOCAL}. Запустите с --fetch.")
    return json.loads(SPEC_LOCAL.read_text(encoding="utf-8"))


def method_from_path(path: str) -> str:
    """Имя метода, выведенное из пути: '/v1/orders.list' -> 'v1.orders.list'."""
    return path.strip("/").replace("/", ".")


def body_method_of(spec: dict, op: dict) -> tuple[Optional[str], str]:
    """Значение поля `method` в теле запроса. Возвращает (значение, источник).

    Приоритет источников — по их доказательной силе:
      1. `enum` в схеме поля `method` (89 операций). Это ОГРАНИЧЕНИЕ: сервер
         проверяет значение, значит оно авторитетно.
      2. `example` (65 операций). Всего лишь иллюстрация, и спека Lamoda
         демонстрирует, что иллюстрации копипастятся между методами.
      3. отсутствует (1 операция) — выводим из пути.
    """
    body = None
    for prm in op.get("parameters") or []:
        if prm.get("in") == "body":
            body = prm
            break
    node = (((body or {}).get("schema") or {}).get("properties") or {}).get("method") or {}
    enum = node.get("enum")
    if isinstance(enum, list) and enum:
        return str(enum[0]), "enum"
    if node.get("example"):
        return str(node["example"]), "example"
    return None, "none"


def normalize_method(name: str) -> str:
    """Привести имя метода к сравнимому виду.

    Спека Lamoda непоследовательна в мелочах: примеры то включают префикс версии
    ('v1.brands.list'), то опускают его ('brands.list'), а вложенность иногда
    записана слэшем вместо точки. Для сравнения «то же это имя или другое» такие
    различия несущественны — важно, совпадает ли сама операция.
    """
    low = name.lower().replace("/", ".").replace("_", "-")
    for prefix in ("v1.", "v2."):
        if low.startswith(prefix):
            low = low[len(prefix):]
    # camelCase -> дефисы, чтобы 'stockReport' сравнивалось со 'stock-report'
    low = re.sub(r"(?<=[a-z])(?=[A-Z])", "-", name).lower().replace("/", ".").replace("_", "-")
    for prefix in ("v1.", "v2."):
        if low.startswith(prefix):
            low = low[len(prefix):]
    return low


def choose_rpc_method(path: str, spec_value: Optional[str], source: str,
                      all_paths: set[str]) -> tuple[str, str, Optional[str], str]:
    """Выбрать значение `method` для тела запроса.

    Возвращает (значение, источник, описание конфликта или None, серьёзность).

    Значение из спеки берётся как лучшее доступное свидетельство: `enum` сервер
    проверяет, `example` написан авторами API. Но расхождения с путём делятся на
    два класса, и смешивать их нельзя:

    - `low`  — различие только в оформлении (префикс версии, camelCase, слэш
      вместо точки). Систематический стиль спеки, не ошибка.
    - `high` — имя указывает на ДРУГУЮ операцию. Показательный случай:
      у `/v1/products.total` пример `products.list`, а `products.list` —
      самостоятельный метод спеки. Либо это копипаста, либо путь и метод
      действительно расходятся; проверить это можно только живым запросом.
    """
    derived = method_from_path(path)
    if not spec_value:
        return derived, "path", None, "none"
    if spec_value == derived:
        return spec_value, source, None, "none"

    same_operation = normalize_method(spec_value) == normalize_method(derived)
    if same_operation:
        return spec_value, source, (
            f"оформление имени в спеке ('{spec_value}') отличается от пути "
            f"('{derived}'); взято значение из спеки"
        ), "low"

    # Имя указывает на другую операцию — самый опасный класс расхождения.
    other_exists = any(
        normalize_method(method_from_path(p)) == normalize_method(spec_value)
        for p in all_paths if p != path
    )
    detail = (
        f"{source} спеки задаёт method '{spec_value}', а путь — '{derived}'. "
        f"Это РАЗНЫЕ операции"
        + (f", причём '{spec_value}' существует в спеке отдельным методом" if other_exists else "")
        + ". Взято значение из спеки; перед реальным вызовом проверьте dry_run."
    )
    return spec_value, source, detail, "high"


def section_for(path: str) -> str:
    low = path.lower()
    for needle, section in SECTION_RULES:
        if needle in low:
            return section
    return "general"


def draft_safety(rpc_method: str) -> str:
    """Черновой уровень по последнему сегменту имени. Может вернуть 'unknown'."""
    last = rpc_method.split(".")[-1]
    # Имена вида 'nomenclatures-update-price' несут глагол в самом сегменте.
    for token in DESTRUCTIVE_SUFFIXES:
        if last == token or last.endswith("-" + token):
            return "destructive"
    if last in READ_SUFFIXES:
        return "read"
    if last in WRITE_SUFFIXES:
        return "write"
    # составные имена: ищем глагол внутри сегмента
    for token in WRITE_SUFFIXES:
        if token in last:
            return "write"
    for token in READ_SUFFIXES:
        if last.endswith(token):
            return "read"
    return "unknown"


def resolve_ref(spec: dict, ref: str) -> dict:
    """Разрешить локальную ссылку '#/definitions/X' либо '#/responses/X'."""
    if not ref.startswith("#/"):
        return {}
    node: Any = spec
    for part in ref[2:].split("/"):
        if not isinstance(node, dict):
            return {}
        node = node.get(part, {})
    return node if isinstance(node, dict) else {}


def derive_items_path(spec: dict, op: dict) -> str:
    """Вывести путь к массиву строк из схемы ответа 200.

    Возвращает 'result.<поле>' для первого массива в схеме result, 'result'
    если сам result — массив, иначе пустую строку (нечего пагинировать).
    """
    responses = op.get("responses") or {}
    ok = responses.get("200") or responses.get(200) or {}
    if "$ref" in ok:
        ok = resolve_ref(spec, ok["$ref"])
    schema = ok.get("schema") or {}
    if "$ref" in schema:
        schema = resolve_ref(spec, schema["$ref"])
    result = (schema.get("properties") or {}).get("result") or {}
    if "$ref" in result:
        result = resolve_ref(spec, result["$ref"])
    if result.get("type") == "array":
        return "result"
    props = result.get("properties") or {}
    # Предпочитаем общепринятые имена, затем любое поле-массив.
    for preferred in ("items", "list", "data", "rows", "products", "orders"):
        node = props.get(preferred)
        if isinstance(node, dict) and node.get("type") == "array":
            return f"result.{preferred}"
    for name, node in props.items():
        if isinstance(node, dict) and node.get("type") == "array":
            return f"result.{name}"
    return ""


def derive_params(spec: dict, op: dict) -> tuple[dict, str]:
    """Вернуть (подсказки по параметрам, стиль пагинации)."""
    body_param = None
    for prm in op.get("parameters") or []:
        if prm.get("in") == "body":
            body_param = prm
            break
    if not body_param:
        return {}, "none"
    schema = body_param.get("schema") or {}
    params_node = (schema.get("properties") or {}).get("params") or {}
    if "$ref" in params_node:
        params_node = resolve_ref(spec, params_node["$ref"])
    props = params_node.get("properties") or {}
    required = params_node.get("required") or []
    hints: dict[str, Any] = {}
    for name, node in props.items():
        if not isinstance(node, dict):
            continue
        entry: dict[str, Any] = {"type": node.get("type", "object")}
        if name in required:
            entry["required"] = True
        if node.get("description"):
            entry["description"] = str(node["description"])[:200]
        if node.get("enum"):
            entry["enum"] = node["enum"][:20]
        hints[name] = entry
    pagination = "page" if ("limit" in props and "page" in props) else "none"
    return hints, pagination


def build_keywords(rpc_method: str, section: str, summary: str) -> list[str]:
    """Двуязычные ключевые слова для поиска."""
    words: list[str] = []
    # токены из имени метода
    for tok in re.split(r"[.\-_/]", rpc_method):
        tok = tok.strip().lower()
        if tok and tok not in ("v1", "v2", "jsonrpc") and len(tok) > 2:
            words.append(tok)
    words.extend(SECTION_KEYWORDS.get(section, []))
    # значимые русские слова из официального описания
    for tok in re.findall(r"[а-яё]{4,}", (summary or "").lower()):
        words.append(tok)
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:20]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true",
                    help="скачать спеку заново вместо локальной копии")
    args = ap.parse_args()

    spec = load_spec(args.fetch)
    paths = spec.get("paths") or {}
    overrides_doc = (
        yaml.safe_load(OVERRIDES_PATH.read_text(encoding="utf-8"))
        if OVERRIDES_PATH.exists() else {}
    ) or {}
    overrides: dict[str, str] = overrides_doc.get("safety") or {}
    summaries: dict[str, str] = overrides_doc.get("summaries") or {}
    extra_internal: set[str] = set(overrides_doc.get("internal") or [])

    records: list[dict] = []
    unknown: list[str] = []
    conflicts: list[str] = []
    escalated: list[str] = []
    all_paths = set(paths.keys())

    for path, item in sorted(paths.items()):
        op = item.get("post") or item.get("get")
        if not op:
            continue
        # operation_id — устойчивый ключ каталога, всегда выводится из пути:
        # путь определяет, КУДА уходит запрос, и не меняется от редакции спеки.
        operation_id = method_from_path(path)
        spec_value, spec_source = body_method_of(spec, op)
        rpc_method, method_source, conflict, severity = choose_rpc_method(
            path, spec_value, spec_source, all_paths
        )
        if conflict and severity == "high":
            conflicts.append(f"{operation_id}: {conflict}")

        section = section_for(path)
        summary = (op.get("description") or op.get("summary") or "").strip()
        if operation_id in summaries:
            summary = summaries[operation_id]
        tags = op.get("tags") or []
        items_path = derive_items_path(spec, op)
        params, pagination = derive_params(spec, op)

        # --- safety --------------------------------------------------------
        # Черновик считаем ПО ОБОИМ именам: по пути и по значению method из
        # спеки. Если они расходятся в опасную сторону, берём строгое.
        # Показательный случай: путь '/v1/nomenclature.validate-price' читается
        # как проверка, но enum спеки требует method 'v1.nomenclature.set-price'
        # — то есть установку цены. Что бы это ни было (ошибка спеки или
        # реальное поведение), пометить такой метод read нельзя.
        draft_by_path = draft_safety(operation_id)
        draft_by_method = draft_safety(rpc_method) if rpc_method != operation_id else draft_by_path
        drafts = [d for d in (draft_by_path, draft_by_method) if d != "unknown"]
        if drafts:
            draft = max(drafts, key=lambda d: SAFETY_RANK[d])
        else:
            draft = "unknown"
        if draft_by_path != draft_by_method and "unknown" not in (
            draft_by_path, draft_by_method
        ):
            escalated.append(
                f"{operation_id}: путь -> {draft_by_path}, method '{rpc_method}' -> "
                f"{draft_by_method}; взято {draft}"
            )

        if operation_id in overrides:
            safety = overrides[operation_id]
        elif draft == "unknown":
            # Не опознано и не курировано — самый строгий разумный уровень.
            safety = "write"
            unknown.append(operation_id)
        else:
            safety = draft

        api_version = "v2" if path.startswith("/v2/") else "v1"

        rec = {
            "operation_id": operation_id,
            "rpc_method": rpc_method,
            "path": f"{BASE_PATH}{path}",
            "section": section,
            "safety": safety,
            "summary": summary or f"Метод Lamoda {operation_id}.",
            "pagination": pagination,
            "items_path": items_path,
            "keywords": build_keywords(operation_id, section, summary),
            "internal": ("internal api" in tags) or (operation_id in extra_internal),
            "api_version": api_version,
            # Каталог собран офлайн: ни один метод не проверялся на живом кабинете.
            "live_verified": False,
        }
        if method_source != "path":
            rec["method_source"] = method_source
        if conflict:
            rec["spec_conflict"] = conflict
            rec["spec_conflict_severity"] = severity
        if params:
            rec["params"] = params
        records.append(rec)

    # Постраничные методы, у которых схема ответа не раскрывает массив строк.
    # Обход всё равно работает — walker перебирает запасные пути и сообщает,
    # какой сработал, — но пробел фиксируется явно, а не замалчивается: именно
    # такие «тихие» места и дают потом «ноль строк без объяснения».
    unresolved = sorted(
        r["operation_id"] for r in records
        if r["pagination"] == "page" and not r["items_path"]
    )

    doc = {
        "service": "lamoda",
        "host": PUBLIC_HOST,
        "base_path": BASE_PATH,
        "protocol": "json-rpc-2.0",
        "generated_from": SPEC_URL,
        "items_path_unresolved": unresolved,
        "endpoints": records,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# СГЕНЕРИРОВАННЫЙ ФАЙЛ — правьте scripts/ingest_lamoda.py или\n"
        "# lamoda_mcp/safety_overrides.yaml, затем пересоберите:\n"
        "#     python3 scripts/ingest_lamoda.py\n"
        f"# Источник: {SPEC_URL}\n"
        "#\n"
        "# live_verified: false у всех методов — каталог собран из спеки,\n"
        "# ни один запрос не выполнялся на живом кабинете Lamoda.\n"
    )
    OUT_PATH.write_text(
        header + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )

    by_safety: dict[str, int] = {}
    by_section: dict[str, int] = {}
    for r in records:
        by_safety[r["safety"]] = by_safety.get(r["safety"], 0) + 1
        by_section[r["section"]] = by_section.get(r["section"], 0) + 1

    print(f"Записано {len(records)} методов в {OUT_PATH}")
    print(f"  safety : {dict(sorted(by_safety.items()))}")
    print(f"  секции : {dict(sorted(by_section.items()))}")
    print(f"  internal: {sum(1 for r in records if r['internal'])}")
    print(f"  v2-пути : {sum(1 for r in records if r['api_version'] == 'v2')}")
    print(f"  с items_path: {sum(1 for r in records if r['items_path'])}")
    print(f"  пагинируемых: {sum(1 for r in records if r['pagination'] == 'page')}")
    high = sum(1 for r in records if r.get("spec_conflict_severity") == "high")
    low = sum(1 for r in records if r.get("spec_conflict_severity") == "low")
    print(f"  расхождения спеки: {high} серьёзных, {low} косметических")
    if unknown:
        print(f"\n  НЕ КУРИРОВАНЫ ({len(unknown)}) — помечены write по умолчанию,")
        print("  впишите их в lamoda_mcp/safety_overrides.yaml:")
        for m in unknown:
            print(f"    {m}")
    if escalated:
        print(f"\n  SAFETY ПОВЫШЕН из-за расхождения путь/method ({len(escalated)}):")
        for m in escalated:
            print(f"    {m}")
    if conflicts:
        print(f"\n  РАСХОЖДЕНИЯ спеки ({len(conflicts)}) — записаны в поле spec_conflict:")
        for m in conflicts:
            print(f"    {m}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
