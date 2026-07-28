"""MCP-сервер для кабинета продавца Lamoda.

Транспорт stdio, FastMCP. Каталог из 155 методов Lamoda Seller Partner API
описан схемой (`endpoints.yaml`), поэтому дженерик-тулы покрывают весь API, а не
только заранее захардкоженную часть.

Набор тулов повторяет проверенный в marketplaces-mcp-ru паттерн: поиск по
каталогу, описание метода, вызов по имени, сырой вызов, автоматический обход
страниц, обзор секций, проверка доступа, управление кабинетами.

ВАЖНО про stdout. Транспорт stdio использует стандартный вывод для протокола
MCP, поэтому в этом процессе нельзя ничего печатать в stdout — любая отладка
идёт в stderr.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Optional

import yaml
from mcp.server.fastmcp import FastMCP

# Пакет должен работать и как установленный модуль, и при запуске из репозитория.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.client import LamodaClient, ServiceConfig  # noqa: E402
from core.credentials import CredentialStore  # noqa: E402
from core.errors import make_error  # noqa: E402
from core.paginate import fetch_all as walk_pages  # noqa: E402
from core.registry import Catalog  # noqa: E402
from core.safety import check_gate  # noqa: E402

HERE = Path(__file__).resolve().parent
CATALOG_PATH = HERE / "endpoints.yaml"
WORKFLOWS_PATH = HERE / "workflows.yaml"

mcp = FastMCP("lamoda")
catalog = Catalog.from_yaml(CATALOG_PATH)
store = CredentialStore()
client = LamodaClient(ServiceConfig(store=store))


def _dump(obj: Any) -> str:
    """Единая сериализация ответа тула."""
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _unknown_method(operation_id: str) -> dict:
    near = [s.operation_id for s in catalog.search(operation_id.replace(".", " "), limit=5)]
    return make_error(
        "not_found",
        f"Метода '{operation_id}' нет в каталоге. Найдите нужный через "
        f"lamoda_search_methods или вызовите произвольный метод через "
        f"lamoda_call_raw.",
        operation_id=operation_id,
        details={"похожие": near} if near else None,
    )


def _spec_warnings(spec) -> list[str]:
    """Предупреждения, которые пользователь должен видеть до вызова."""
    out: list[str] = []
    if not spec.live_verified:
        out.append(
            "Метод не проверялся на живом кабинете Lamoda: параметры выведены из "
            "официальной спеки. Перед первым реальным вызовом полезен dry_run=true."
        )
    if spec.spec_conflict and spec.spec_conflict_severity == "high":
        out.append(f"Расхождение в спеке Lamoda: {spec.spec_conflict}")
    if spec.internal:
        out.append(
            "Метод помечен как внутренний — он не предназначен обычному продавцу "
            "и может быть недоступен по вашему доступу."
        )
    return out


# --------------------------------------------------------------------------
# Обзор каталога
# --------------------------------------------------------------------------
@mcp.tool()
def lamoda_search_methods(query: str, limit: int = 15,
                          include_internal: bool = False) -> str:
    """Найти методы Lamoda по запросу на русском или английском.

    Ищет по имени метода, описанию, разделу и ключевым словам. Начинайте с этого
    тула, когда не знаете точного имени: «остатки», «заказы за неделю», «цены».

    include_internal=true добавляет служебные методы Lamoda, которые обычному
    продавцу не нужны.
    """
    found = catalog.search(query, limit=limit, include_internal=include_internal)
    return _dump({
        "query": query,
        "found": len(found),
        "methods": [s.to_summary_dict() for s in found],
        "hint": "Подробности по методу — lamoda_describe_method(operation_id).",
    })


@mcp.tool()
def lamoda_describe_method(operation_id: str) -> str:
    """Показать подробности метода: параметры, раздел, уровень доступа, предупреждения.

    Вызывайте перед первым обращением к незнакомому методу — здесь видно, какие
    параметры обязательны и подтверждён ли метод живым запросом.
    """
    spec = catalog.get(operation_id)
    if not spec:
        return _dump(_unknown_method(operation_id))
    out: dict[str, Any] = {
        "operation_id": spec.operation_id,
        "rpc_method": spec.rpc_method,
        "path": spec.path,
        "section": spec.section,
        "safety": spec.safety,
        "summary": spec.summary,
        "pagination": spec.pagination,
        "items_path": spec.items_path or "(массив строк не обнаружен в схеме)",
        "api_version": spec.api_version,
        "live_verified": spec.live_verified,
        "params": spec.params or "(схема параметров в спеке не описана)",
    }
    warnings = _spec_warnings(spec)
    if warnings:
        out["warnings"] = warnings
    if spec.safety != "read":
        out["confirmation_required"] = (
            ["confirm_write=true"] if spec.safety == "write"
            else ["confirm_write=true", "i_understand_this_modifies_data=true"]
        )
    return _dump(out)


@mcp.tool()
def lamoda_list_sections() -> str:
    """Показать разделы каталога и число методов в каждом."""
    titles = {
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
        "content": "Контент и изображения",
        "validation": "Валидация",
        "general": "Прочее",
    }
    sections = catalog.sections()
    return _dump({
        "total_methods": len(catalog.all()),
        "safety": catalog.counts_by_safety(),
        "sections": [
            {"section": key, "title": titles.get(key, key), "methods": count}
            for key, count in sections.items()
        ],
    })


@mcp.tool()
def lamoda_get_section(section: str, include_internal: bool = False) -> str:
    """Показать все методы раздела (например «orders», «stocks», «prices»)."""
    specs = catalog.in_section(section)
    if not specs:
        return _dump(make_error(
            "not_found",
            f"Раздела '{section}' нет. Доступные: "
            f"{', '.join(catalog.sections().keys())}",
        ))
    if not include_internal:
        specs = [s for s in specs if not s.internal]
    return _dump({
        "section": section,
        "methods": [s.to_summary_dict() for s in sorted(specs, key=lambda x: x.operation_id)],
    })


# --------------------------------------------------------------------------
# Вызов
# --------------------------------------------------------------------------
@mcp.tool()
async def lamoda_call_method(
    operation_id: str,
    params: Optional[dict] = None,
    confirm_write: bool = False,
    i_understand_this_modifies_data: bool = False,
    dry_run: bool = False,
) -> str:
    """Вызвать метод Lamoda из каталога.

    Чтение выполняется сразу. Запись требует confirm_write=true, а необратимые
    операции — дополнительно i_understand_this_modifies_data=true.

    dry_run=true возвращает точное тело запроса, ничего не отправляя. Каталог
    собран из спеки и не подтверждён живыми вызовами, поэтому для пишущих
    методов это разумный первый шаг.
    """
    spec = catalog.get(operation_id)
    if not spec:
        return _dump(_unknown_method(operation_id))

    if not dry_run:
        blocked = check_gate(
            spec.safety,
            confirm_write=confirm_write,
            i_understand_this_modifies_data=i_understand_this_modifies_data,
            operation_id=operation_id,
            live_verified=spec.live_verified,
        )
        if blocked:
            return _dump(blocked)

    result = await client.call_spec(spec, params or {}, dry_run=dry_run)
    warnings = _spec_warnings(spec)
    if warnings and isinstance(result, dict):
        result.setdefault("warnings", warnings)
    return _dump(result)


@mcp.tool()
async def lamoda_call_raw(
    rpc_method: str,
    params: Optional[dict] = None,
    confirm_write: bool = False,
    i_understand_this_modifies_data: bool = False,
    dry_run: bool = False,
) -> str:
    """Вызвать ЛЮБОЙ метод Lamoda по имени, даже отсутствующий в каталоге.

    Запасной путь на случай, когда Lamoda добавила метод, а каталог ещё не
    пересобран. Имя указывается как в документации: «v1.orders.list».

    Уровень доступа для неизвестного каталогу метода определить неоткуда,
    поэтому он считается пишущим и требует confirm_write=true. Известные
    методы проверяются по каталогу.
    """
    spec = catalog.get(rpc_method)
    safety = spec.safety if spec else "write"
    live_verified = spec.live_verified if spec else False

    if not dry_run:
        blocked = check_gate(
            safety,
            confirm_write=confirm_write,
            i_understand_this_modifies_data=i_understand_this_modifies_data,
            operation_id=rpc_method,
            live_verified=live_verified,
        )
        if blocked:
            if not spec:
                blocked["message"] += (
                    " Метода нет в каталоге, поэтому он считается пишущим — "
                    "проверьте имя через lamoda_search_methods."
                )
            return _dump(blocked)

    result = await client.call(
        spec.rpc_method if spec else rpc_method,
        params or {},
        operation_id=rpc_method,
        # Для известного метода путь берём из каталога: у части методов он
        # содержит вложенные сегменты и из имени не выводится.
        path=spec.path if spec else None,
        retry_safe=(safety == "read"),
        dry_run=dry_run,
    )
    return _dump(result)


@mcp.tool()
async def lamoda_fetch_all(
    operation_id: str,
    params: Optional[dict] = None,
    page_size: int = 100,
    max_items: int = 10000,
    max_pages: int = 200,
) -> str:
    """Собрать все страницы метода-списка в один результат.

    Работает только с чтением: автоматический обход пишущего метода повторил бы
    изменение на каждой странице.

    Если вернулось ноль строк, в ответе будет указано, по какому пути искали
    массив, — «молча ноль» отличается от «данных нет».
    """
    spec = catalog.get(operation_id)
    if not spec:
        return _dump(_unknown_method(operation_id))
    if spec.safety != "read":
        return _dump(make_error(
            "safety_gate",
            f"lamoda_fetch_all работает только с чтением, а '{operation_id}' "
            f"помечен как {spec.safety}. Постраничный обход применил бы изменение "
            f"многократно. Ничего не отправлено.",
            operation_id=operation_id,
            details={"http_call_skipped": True},
        ))
    if spec.pagination != "page":
        return _dump(make_error(
            "invalid_params",
            f"Метод '{operation_id}' не постраничный (pagination={spec.pagination}). "
            f"Вызовите его напрямую через lamoda_call_method.",
            operation_id=operation_id,
        ))
    result = await walk_pages(
        client, spec, params=params or {}, limit=page_size,
        max_items=max_items, max_pages=max_pages,
    )
    return _dump(result)


# --------------------------------------------------------------------------
# Доступ и кабинеты
# --------------------------------------------------------------------------
@mcp.tool()
async def lamoda_check_auth() -> str:
    """Проверить, что креды на месте и Lamoda выдаёт токен.

    Делает один реальный запрос за токеном и показывает выданные права (scope).
    С этого стоит начинать при любой непонятной ошибке доступа.
    """
    missing = store.missing()
    cabinets = store.list_cabinets()
    if missing:
        return _dump(make_error(
            "auth",
            f"Не заданы креды: {', '.join(missing)}. Добавьте кабинет через "
            f"lamoda_add_cabinet или задайте LAMODA_CLIENT_ID и "
            f"LAMODA_CLIENT_SECRET.",
            details={"кабинеты": cabinets},
        ))
    creds, source = store.resolve()
    result = await client._rpc_raw(  # noqa: SLF001 — намеренная проверка контракта токена
        "v1.tokens.create",
        {
            "clientId": creds.get("client_id", ""),
            "clientSecret": creds.get("client_secret", ""),
            "grantType": "client_credentials",
        },
    )
    if not result.get("ok"):
        result.setdefault("details", {})
        return _dump({
            "ok": False,
            "источник_кредов": source,
            "кабинеты": cabinets,
            "ошибка": result,
            "подсказка": (
                "Активный кабинет имеет приоритет над переменными окружения. "
                "Если ключи в env верные, проверьте, не активен ли кабинет с "
                "устаревшими ключами."
            ),
        })
    data = result.get("data") or {}
    return _dump({
        "ok": True,
        "источник_кредов": source,
        "кабинеты": cabinets,
        "scope": data.get("scope", "(не указан)"),
        "token_type": data.get("tokenType"),
        "expires_in": data.get("expiresIn"),
        "методов_в_каталоге": len(catalog.all()),
    })


@mcp.tool()
def lamoda_list_cabinets() -> str:
    """Показать сохранённые кабинеты Lamoda и активный из них."""
    return _dump(store.list_cabinets())


@mcp.tool()
def lamoda_add_cabinet(name: str, client_id: str, client_secret: str,
                       make_active: bool = True) -> str:
    """Сохранить креды кабинета Lamoda под именем.

    Ключи пишутся в ~/.lamoda-mcp/cabinets.json с правами 600 и никогда не
    возвращаются обратно в чат.
    """
    if not client_id or not client_secret:
        return _dump(make_error("invalid_params",
                                "Нужны и client_id, и client_secret."))
    store.add_cabinet(name, {"client_id": client_id, "client_secret": client_secret},
                      make_active=make_active)
    return _dump({
        "ok": True,
        "сохранён": name,
        "активный": store.list_cabinets().get("active"),
        "примечание": "Ключи записаны локально. Проверьте доступ: lamoda_check_auth.",
    })


@mcp.tool()
def lamoda_use_cabinet(name: str) -> str:
    """Переключиться на сохранённый кабинет Lamoda."""
    if not store.set_active(name):
        return _dump(make_error(
            "not_found",
            f"Кабинета '{name}' нет. Доступные: "
            f"{', '.join(store.list_cabinets()['cabinets']) or 'ни одного'}",
        ))
    return _dump({"ok": True, "активный": name})


# --------------------------------------------------------------------------
# Рецепты
# --------------------------------------------------------------------------
def _load_workflows() -> dict:
    if not WORKFLOWS_PATH.exists():
        return {}
    return yaml.safe_load(WORKFLOWS_PATH.read_text(encoding="utf-8")) or {}


@mcp.tool()
def lamoda_list_workflows() -> str:
    """Показать готовые бизнес-рецепты (ABC-анализ, покрытие остатков и другие)."""
    flows = _load_workflows().get("workflows", {})
    return _dump({
        "workflows": [
            {"name": k, "title": v.get("title", k), "question": v.get("question", "")}
            for k, v in flows.items()
        ],
        "hint": "Подробности — lamoda_get_workflow(name).",
    })


@mcp.tool()
def lamoda_get_workflow(name: str) -> str:
    """Показать пошаговый рецепт: какие методы вызвать и как посчитать результат."""
    flows = _load_workflows().get("workflows", {})
    flow = flows.get(name)
    if not flow:
        return _dump(make_error(
            "not_found",
            f"Рецепта '{name}' нет. Доступные: {', '.join(flows.keys())}",
        ))
    return _dump(flow)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
