"""Каталог методов, управляемый схемой.

Каталог — источник истины о том, что умеет сервер. Он лежит в `endpoints.yaml`
и генерируется из официальной спеки Lamoda скриптом `scripts/ingest_lamoda.py`.
Дженерик-исполнитель умеет вызвать ЛЮБУЮ запись каталога по `operation_id`, а
`call_raw` — любой метод вообще, даже отсутствующий в каталоге. За счёт этого
покрытие полное с первого дня, без 155 захардкоженных тулов.

Запись каталога:

    operation_id: v1.orders.list      # уникальный; он же имя метода JSON-RPC
    rpc_method:   v1.orders.list      # что кладём в поле "method" конверта
    path:         /jsonrpc/v1/orders.list
    section:      orders              # бизнес-группировка для поиска и обзора
    safety:       read                # read | write | destructive
    summary:      Список заказов партнёра.
    pagination:   page                # page | none
    items_path:   result.items        # где в ответе лежит массив строк
    keywords:     [заказы, orders]    # двуязычный поиск
    internal:     false               # метод с тегом "internal api" — не для продавца
    api_version:  v1                  # внутри v1-спеки встречаются пути /v2/*
    live_verified: false              # проверялся ли метод на живом кабинете

Про `items_path` и `pagination`. Оба поля выведены из схем ответа официальной
спеки. Это ОБОСНОВАННЫЙ СТАРТОВЫЙ ДЕФОЛТ, а не гарантия: в marketplace-mcp схема
WB обещала `result.items`, а живой ответ отдавал `data.listGoods`, из-за чего
обход страниц молча возвращал ноль строк. Пока по методу не прошёл живой запрос,
`live_verified` остаётся false.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

SAFETY_LEVELS = ("read", "write", "destructive")


@dataclass
class EndpointSpec:
    operation_id: str
    path: str
    rpc_method: str = ""
    section: str = "general"
    safety: str = "read"
    summary: str = ""
    pagination: str = "none"
    items_path: str = ""
    keywords: list[str] = field(default_factory=list)
    # подсказки по параметрам, показываются в describe_method
    params: dict[str, Any] = field(default_factory=dict)
    internal: bool = False
    api_version: str = "v1"
    live_verified: bool = False
    doc: str = ""
    # Откуда взято значение поля `method` в теле запроса: enum | example | path.
    # `enum` — ограничение, проверяемое сервером; `example` — лишь иллюстрация.
    method_source: str = "path"
    # Заполняется, когда имя метода в спеке расходится с путём. Severity:
    # "low" — различие в оформлении (префикс версии, camelCase);
    # "high" — имя указывает на другую операцию, требуется живая проверка.
    spec_conflict: str = ""
    spec_conflict_severity: str = ""

    def __post_init__(self) -> None:
        # Имя метода по умолчанию совпадает с operation_id — так в спеке Lamoda.
        if not self.rpc_method:
            self.rpc_method = self.operation_id

    @property
    def path_params(self) -> list[str]:
        return re.findall(r"\{([^}]+)\}", self.path)

    def to_summary_dict(self) -> dict:
        out = {
            "operation_id": self.operation_id,
            "section": self.section,
            "safety": self.safety,
            "summary": self.summary,
            "pagination": self.pagination,
            "live_verified": self.live_verified,
        }
        if self.internal:
            out["internal"] = True
        if self.api_version != "v1":
            out["api_version"] = self.api_version
        if self.spec_conflict_severity == "high":
            # Серьёзное расхождение должно быть видно уже в списке, а не только
            # в подробном описании метода.
            out["spec_conflict"] = self.spec_conflict
        return out


class Catalog:
    """Загруженный и доступный для поиска набор EndpointSpec."""

    def __init__(self, specs: list[EndpointSpec], meta: Optional[dict] = None):
        self._by_id: dict[str, EndpointSpec] = {s.operation_id: s for s in specs}
        # Сведения о каталоге в целом: хост, источник, зафиксированные пробелы.
        self.meta: dict[str, Any] = meta or {}

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Catalog":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        specs = [EndpointSpec(**rec) for rec in raw.get("endpoints", [])]
        meta = {k: v for k, v in raw.items() if k != "endpoints"}
        return cls(specs, meta=meta)

    @property
    def items_path_unresolved(self) -> list[str]:
        """Постраничные методы, для которых схема не раскрыла путь к строкам.

        Обход по ним работает через запасные пути, но это известный пробел:
        закрывается живой валидацией (scripts/validate_items_path.py).
        """
        return list(self.meta.get("items_path_unresolved") or [])

    def get(self, operation_id: str) -> Optional[EndpointSpec]:
        return self._by_id.get(operation_id)

    def all(self) -> list[EndpointSpec]:
        return list(self._by_id.values())

    def sections(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self._by_id.values():
            out[s.section] = out.get(s.section, 0) + 1
        return dict(sorted(out.items()))

    def in_section(self, section: str) -> list[EndpointSpec]:
        return [s for s in self._by_id.values() if s.section == section]

    def counts_by_safety(self) -> dict[str, int]:
        out = {lvl: 0 for lvl in SAFETY_LEVELS}
        for s in self._by_id.values():
            out[s.safety] = out.get(s.safety, 0) + 1
        return out

    def search(self, query: str, limit: int = 15,
               include_internal: bool = False) -> list[EndpointSpec]:
        """Поиск по совпадению токенов: имя метода, описание, секция, ключевые слова.

        Ключевые слова двуязычные, поэтому русский запрос («остатки», «заказы»)
        находит методы с английскими именами.
        """
        terms = [t for t in re.split(r"[^\w]+", query.lower()) if t]
        if not terms:
            return []
        scored: list[tuple[float, EndpointSpec]] = []
        for s in self._by_id.values():
            if s.internal and not include_internal:
                continue
            hay = " ".join(
                [s.operation_id, s.summary, s.path, s.section] + s.keywords
            ).lower()
            score = 0.0
            for t in terms:
                if t in hay:
                    score += 1.0
                if t in s.operation_id.lower():
                    score += 0.5
                if t == s.section.lower():
                    score += 0.5
            if score > 0:
                scored.append((score, s))
        scored.sort(key=lambda x: (-x[0], x[1].operation_id))
        return [s for _, s in scored[:limit]]
