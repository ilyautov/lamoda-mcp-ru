#!/usr/bin/env python3
"""Проверить каталог живыми запросами и починить items_path по фактическим ответам.

ЗАЧЕМ. Каталог собран из официальной спеки Lamoda, но спека не равна бою. В
marketplace-mcp схема WB обещала массив по пути `result.items`, а живой ответ
отдавал `data.listGoods` — обход страниц молча возвращал ноль строк, и заметили
это только живым прогоном. Здесь та же ситуация: `items_path` и формы `params`
выведены из схем и до первого реального вызова остаются гипотезой.

ЧТО ДЕЛАЕТ. Дёргает read-методы каталога с минимальным телом, смотрит на
фактический ответ и сообщает:
  - какие методы ответили успешно;
  - где объявленный items_path не совпал с реальным расположением массива;
  - какие методы недоступны по текущим правам (scope).
С флагом --fix однозначные промахи (в ответе ровно один массив) правятся в
endpoints.yaml, а методы, ответившие успешно, помечаются live_verified: true.

ЗАПУСКАТЬ ЛОКАЛЬНО, не в песочнице агента. Урок marketplace-mcp: широкий live-
свип внешнего API упирается в лимит времени и в rate limit — каждый ответ 429
съедает секунды. На своей машине лимита времени нет и бюджет запросов свежий.

    export LAMODA_CLIENT_ID=...
    export LAMODA_CLIENT_SECRET=...
    python3 scripts/validate_items_path.py                 # только отчёт
    python3 scripts/validate_items_path.py --fix           # + починить каталог
    python3 scripts/validate_items_path.py --section stocks --delay 1.0
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any, Optional

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.client import LamodaClient, ServiceConfig  # noqa: E402
from core.registry import Catalog  # noqa: E402

CATALOG_PATH = ROOT / "lamoda_mcp" / "endpoints.yaml"


def find_arrays(obj: Any, prefix: str = "") -> list[tuple[str, int]]:
    """Все массивы в ответе: [(путь, длина)]. Обходит только словари."""
    out: list[tuple[str, int]] = []
    if isinstance(obj, list):
        out.append((prefix or "<корень>", len(obj)))
        return out
    if not isinstance(obj, dict):
        return out
    for key, value in obj.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, list):
            out.append((path, len(value)))
        elif isinstance(value, dict):
            out.extend(find_arrays(value, path))
    return out


async def probe(client: LamodaClient, spec, delay: float) -> dict:
    """Один осторожный вызов метода. Тело — минимальное."""
    params: dict[str, Any] = {}
    if spec.pagination == "page":
        params = {"limit": 1, "page": 1}
    result = await client.call_spec(spec, params)
    await asyncio.sleep(delay)  # бережём лимит запросов

    if not result.get("ok"):
        return {
            "operation_id": spec.operation_id,
            "ok": False,
            "error_type": result.get("error_type"),
            "message": (result.get("message") or "")[:200],
        }

    data = result.get("data")
    arrays = find_arrays(data)
    declared = spec.items_path
    declared_ok = False
    if declared:
        cur: Any = data
        for part in declared.split("."):
            # items_path записан относительно result, который уже распакован
            if part == "result":
                continue
            cur = cur.get(part) if isinstance(cur, dict) else None
        declared_ok = isinstance(cur, list)

    return {
        "operation_id": spec.operation_id,
        "ok": True,
        "declared": declared or "(не задан)",
        "declared_ok": declared_ok,
        "arrays": arrays,
        "top_keys": sorted(data.keys()) if isinstance(data, dict) else type(data).__name__,
    }


def suggest(report: dict) -> Optional[str]:
    """Однозначная замена items_path: ровно один массив в ответе."""
    if not report.get("ok") or report.get("declared_ok"):
        return None
    arrays = report.get("arrays") or []
    if len(arrays) != 1:
        return None
    path = arrays[0][0]
    return path if path.startswith("result") else f"result.{path}"


async def run(args) -> int:
    catalog = Catalog.from_yaml(CATALOG_PATH)
    client = LamodaClient(ServiceConfig())

    specs = [s for s in catalog.all() if s.safety == "read" and not s.internal]
    if args.section:
        specs = [s for s in specs if s.section == args.section]
    specs.sort(key=lambda s: s.operation_id)
    if args.limit:
        specs = specs[: args.limit]

    print(f"Проверяю {len(specs)} read-методов, пауза {args.delay}с между вызовами.\n")

    reports: list[dict] = []
    for i, spec in enumerate(specs, 1):
        rep = await probe(client, spec, args.delay)
        reports.append(rep)
        mark = "ok " if rep["ok"] else "ERR"
        extra = ""
        if rep["ok"] and not rep.get("declared_ok"):
            fix = suggest(rep)
            extra = f"  items_path: {rep['declared']} -> {fix or '???'}"
        elif not rep["ok"]:
            extra = f"  {rep['error_type']}: {rep['message'][:80]}"
        print(f"[{i:3}/{len(specs)}] {mark} {spec.operation_id}{extra}")

    ok = [r for r in reports if r["ok"]]
    bad_path = [r for r in ok if not r.get("declared_ok")]
    fixable = {r["operation_id"]: suggest(r) for r in bad_path if suggest(r)}

    print("\n" + "=" * 60)
    print(f"Успешных вызовов : {len(ok)} из {len(reports)}")
    print(f"items_path неверен: {len(bad_path)}")
    print(f"Чинится однозначно: {len(fixable)}")
    errors: dict[str, int] = {}
    for r in reports:
        if not r["ok"]:
            errors[r["error_type"]] = errors.get(r["error_type"], 0) + 1
    if errors:
        print(f"Ошибки по типам   : {errors}")

    if not args.fix:
        print("\nЭто был только отчёт. Запустите с --fix, чтобы записать правки.")
        return 0

    doc = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8"))
    verified = {r["operation_id"] for r in ok}
    changed = 0
    for rec in doc["endpoints"]:
        oid = rec["operation_id"]
        if oid in fixable:
            rec["items_path"] = fixable[oid]
            changed += 1
        if oid in verified:
            # Метод реально ответил — теперь это факт, а не вывод из спеки.
            rec["live_verified"] = True
    doc["items_path_unresolved"] = sorted(
        r["operation_id"] for r in doc["endpoints"]
        if r["pagination"] == "page" and not r["items_path"]
    )
    CATALOG_PATH.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    print(f"\nЗаписано: items_path исправлен у {changed}, "
          f"live_verified проставлен у {len(verified)}.")
    print("Перепроверьте: python3 scripts/audit_safety.py && pytest -q")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true", help="записать правки в каталог")
    ap.add_argument("--section", default="", help="проверить только один раздел")
    ap.add_argument("--limit", type=int, default=0, help="ограничить число методов")
    ap.add_argument("--delay", type=float, default=0.5,
                    help="пауза между вызовами, секунд")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
