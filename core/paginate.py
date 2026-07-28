"""Обход страниц, чтобы агент мог попросить «всё» и не писать цикл руками.

Lamoda использует постраничную схему: в `params` кладутся `limit` и `page`
(нумерация с 1). Это отличается от WB/Ozon, где были cursor, last_id и
lastchangedate.

Поддерживаемые стили (EndpointSpec.pagination):
- page : limit + page, страницы с первой; стоп по пустой странице либо по
         достижению заявленного общего количества
- none : один запрос

Где в ответе лежит массив строк, задаёт `EndpointSpec.items_path`. Это значение
выведено из схемы ответа официальной спеки и является СТАРТОВЫМ ДЕФОЛТОМ, а не
гарантией: в marketplace-mcp схема WB обещала `result.items`, а живой ответ
отдавал `data.listGoods`, из-за чего обход молча возвращал ноль строк. Поэтому
при пустом первом результате walker явно сообщает, по какому пути искал, —
чтобы «молча ноль» нельзя было спутать с «данных нет».
"""
from __future__ import annotations

from typing import Any, Optional

from .client import LamodaClient
from .registry import EndpointSpec

DEFAULT_MAX_ITEMS = 10_000
DEFAULT_MAX_PAGES = 200
DEFAULT_PAGE_SIZE = 100

# Поля, в которых Lamoda может отдавать общее количество строк. Проверяются по
# порядку; первое найденное число используется как условие остановки.
_TOTAL_FIELDS = ("total", "totalCount", "total_count", "count")


def _dig(obj: Any, dotted: str) -> Any:
    cur = obj
    for part in dotted.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _to_int(value: Any) -> Optional[int]:
    """Целое из поля ответа, либо None. Мусорное значение ухудшает условие
    остановки, но не роняет обход."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _find_items(data: Any, items_path: str) -> tuple[Optional[list], str]:
    """Достать массив строк. Возвращает (строки, фактически_сработавший_путь).

    Порядок: сначала объявленный путь; если по нему массива нет — распространённые
    запасные варианты; если ответ сам массив — он и есть строки. Запасные пути
    существуют потому, что items_path выведен из схемы и может расходиться с
    живым ответом; фактический путь возвращается наружу, чтобы расхождение было
    видно, а не молчаливо.
    """
    if items_path:
        found = _dig(data, items_path)
        if isinstance(found, list):
            return found, items_path
    if isinstance(data, list):
        return data, "<корень ответа>"
    for candidate in ("items", "result.items", "list", "data", "rows"):
        found = _dig(data, candidate)
        if isinstance(found, list):
            return found, candidate
    return None, items_path


async def fetch_all(
    client: LamodaClient,
    spec: EndpointSpec,
    *,
    params: Optional[dict[str, Any]] = None,
    items_path: Optional[str] = None,
    limit: int = DEFAULT_PAGE_SIZE,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> dict:
    """Пройти страницы до конца, до обрезания или до лимитов.

    Успех -> {"ok": True, "items": [...], "total_fetched": n, "pages_fetched": p,
              "truncated": bool, "items_path_used": str}
    Ошибка -> канонический конверт с первой неудачи.
    """
    style = spec.pagination or "none"
    declared_path = items_path or spec.items_path
    base = dict(params or {})
    items: list[Any] = []
    pages = 0
    used_path = declared_path

    while True:
        page_params = dict(base)
        if style == "page":
            page_params.setdefault("limit", limit)
            page_params["page"] = pages + 1

        resp = await client.call_spec(spec, page_params)
        if not resp.get("ok"):
            return resp  # пробрасываем конверт ошибки

        data = resp["data"]
        page_items, used_path = _find_items(data, declared_path)
        page_items = page_items or []
        items.extend(page_items)
        pages += 1

        if len(items) >= max_items or pages >= max_pages:
            return _result(items[:max_items], pages, truncated=True,
                           items_path=used_path, declared=declared_path, data=data)
        if not page_items:
            return _result(items, pages, truncated=False,
                           items_path=used_path, declared=declared_path, data=data)
        if style != "page":
            return _result(items, pages, truncated=False,
                           items_path=used_path, declared=declared_path, data=data)

        # Стоп по заявленному общему количеству, если оно есть.
        for fld in _TOTAL_FIELDS:
            total = _to_int(_dig(data, fld))
            if total is None:
                total = _to_int(_dig(data, f"result.{fld}"))
            if total is not None:
                if len(items) >= total:
                    return _result(items, pages, truncated=False,
                                   items_path=used_path, declared=declared_path,
                                   data=data)
                break


def _result(items: list[Any], pages: int, *, truncated: bool, items_path: str,
            declared: str, data: Any) -> dict:
    out = {
        "ok": True,
        "items": items,
        "total_fetched": len(items),
        "pages_fetched": pages,
        "truncated": truncated,
        "items_path_used": items_path,
    }
    if items_path != declared:
        # Расхождение схемы с живым ответом — ровно тот случай, который в
        # marketplace-mcp дал «молча ноль строк». Говорим о нём вслух.
        out["warning"] = (
            f"Массив строк найден по пути '{items_path}', хотя каталог объявляет "
            f"'{declared}'. Живой ответ расходится со спекой — стоит поправить "
            f"items_path в endpoints.yaml."
        )
    if not items:
        out["note"] = (
            f"Ноль строк. Искали по пути '{declared}'. Если данные в кабинете "
            f"есть, проверьте фактическую форму ответа через describe_method "
            f"и вызов с dry_run, затем поправьте items_path. Ключи верхнего "
            f"уровня в ответе: {sorted(data.keys()) if isinstance(data, dict) else type(data).__name__}"
        )
    return out
