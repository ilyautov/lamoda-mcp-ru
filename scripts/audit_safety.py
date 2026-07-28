#!/usr/bin/env python3
"""Аудит каталога: не помечена ли мутация как чтение.

Зачем нужен отдельный аудит. Классификация по имени метода уже дважды подводила
в marketplace-mcp: подстрока `count` ловилась в слове «disCount», `stat` — в
«status», а `import/info` и `cancel/status` оказывались чтением, хотя выглядели
мутациями. Здесь ситуация опаснее: у Lamoda все методы POST, поэтому проверить
классификацию по HTTP-глаголу невозможно.

Скрипт ищет два класса проблем:
  1. метод с глаголом-мутацией в имени, помеченный `read`;
  2. метод, у которого имя в теле запроса (rpc_method) выглядит мутацией,
     хотя путь — чтением (случай `validate-price` -> `set-price`).

Выход: 0 — чисто, 1 — есть подозрения. Годится и для CI.

    python3 scripts/audit_safety.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "lamoda_mcp" / "endpoints.yaml"

# Глаголы, означающие изменение состояния. Проверяются как отдельные сегменты
# имени, а НЕ подстрокой: подстрочный поиск и был источником ложных срабатываний
# (`count` внутри `disCount`, `stat` внутри `status`).
MUTATION_VERBS = {
    "create", "update", "delete", "remove", "cancel", "set", "add", "store",
    "approve", "upload", "import", "apply", "send", "close", "reset",
    "activate", "deactivate", "hide", "confirm", "generate", "first-login",
}

# Сегменты, которые ВЫГЛЯДЯТ мутацией, но означают чтение состояния операции.
# Ровно та ловушка, на которой в marketplace-mcp дважды ошиблась эвристика.
READ_EXCEPTIONS = {
    "status", "statuses", "info", "history", "check", "list", "get", "total",
    "report", "summary", "count", "count-by-statuses", "status-history",
}


def segments(name: str) -> list[str]:
    """Разбить имя метода на сегменты по точкам и дефисам."""
    out: list[str] = []
    for dotted in name.split("."):
        out.extend(p for p in dotted.split("-") if p)
    return out


def looks_mutating(name: str) -> tuple[bool, str]:
    """Похоже ли имя на мутацию. Возвращает (да/нет, объяснение)."""
    last = name.split(".")[-1]
    # Хвост вида '.status', '.list', '.get' означает чтение, даже если раньше
    # в имени встречается глагол ('products-added.report').
    if last in READ_EXCEPTIONS:
        return False, ""
    parts = segments(name)
    for verb in MUTATION_VERBS:
        if verb in parts:
            # 'cancel-status' и 'import-info' — чтение о мутации, не мутация.
            if any(exc in parts for exc in READ_EXCEPTIONS):
                continue
            return True, verb
    return False, ""


def main() -> int:
    doc = yaml.safe_load(CATALOG.read_text(encoding="utf-8"))
    endpoints = doc["endpoints"]

    mislabeled: list[str] = []
    path_method_split: list[str] = []

    for ep in endpoints:
        oid = ep["operation_id"]
        rpc = ep.get("rpc_method", oid)
        safety = ep["safety"]

        mutating_path, verb_path = looks_mutating(oid)
        mutating_rpc, verb_rpc = looks_mutating(rpc)

        if safety == "read" and mutating_path:
            mislabeled.append(f"{oid}: помечен read, но в имени глагол '{verb_path}'")
        if safety == "read" and mutating_rpc and not mutating_path:
            path_method_split.append(
                f"{oid}: путь читается как чтение, но method '{rpc}' содержит "
                f"'{verb_rpc}' — помечен read"
            )

    counts: dict[str, int] = {}
    for ep in endpoints:
        counts[ep["safety"]] = counts.get(ep["safety"], 0) + 1

    print(f"Каталог: {len(endpoints)} методов, {counts}")
    print(f"  internal: {sum(1 for e in endpoints if e.get('internal'))}")
    print(f"  серьёзных расхождений спеки: "
          f"{sum(1 for e in endpoints if e.get('spec_conflict_severity') == 'high')}")

    problems = mislabeled + path_method_split
    if not problems:
        print("\n✓ Мутаций, помеченных read, не найдено.")
        return 0

    print(f"\n✗ Подозрений: {len(problems)}")
    for p in problems:
        print(f"    {p}")
    print("\nИсправьте в lamoda_mcp/safety_overrides.yaml и пересоберите каталог.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
