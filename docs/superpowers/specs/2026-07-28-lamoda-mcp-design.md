# lamoda-mcp-ru — дизайн v1

Дата: 2026-07-28
Статус: утверждён, в реализации
Образец архитектуры: `marketplaces-mcp-ru` (WB + Ozon + Ozon Performance), `realtycalendar-mcp`

## 1. Что строим

MCP-сервер для кабинета продавца Lamoda. Один процесс, транспорт stdio, FastMCP.
Даёт ИИ-агенту (Claude Desktop/Code/Cowork, Codex, OpenCode, Cursor) прямой доступ
к Lamoda Seller Partner API — товары, номенклатура, цены, заказы, отгрузки,
остатки, акции, возвраты, склады, статистика.

## 2. Разведка: что установлено фактами

Проверено 2026-07-28 прямыми запросами, не по памяти:

| Факт | Значение | Как установлено |
|---|---|---|
| Публичная документация | `academy.lamoda.ru/articles/api/` | fetch |
| Машиночитаемая спека | `public-api-seller.lamoda.ru/swagger.json` — 200, 700 КБ | curl |
| Формат спеки | Swagger 2.0, 155 путей, 155 операций, 424 определения, 96 именованных ответов | разбор |
| Протокол | JSON-RPC 2.0 поверх HTTP | спека |
| Публичный хост | `public-api-seller.lamoda.ru`, basePath `/jsonrpc` | доки + спека |
| Хост в спеке | `seller-gateway.service.lamoda.tech` (внутренний, НЕ использовать) | спека |
| Русские описания | у 78 из 155 операций | разбор |
| Пагинация | `limit` (58 определений) + `page` (48), `offset` (7) | разбор |
| Достижимость из песочницы | да, 200 | curl |
| Готовые Lamoda MCP | не найдены — ниша свободна | поиск |

### Поверхности API, которые НЕ берём в v1

- **B2B Platform API** (`api-b2b.lamoda.ru`, REST) — заказы/отгрузки/ярлыки/вебхуки.
  Публичной машиночитаемой спеки нет: `/openapi.json`, `/swagger.json`, `/api/docs`,
  `/swagger/doc.json` и ещё 4 пути → 404; `/health` → 401 (хост жив, всё под авторизацией).
  Строить каталог по тексту доков = tier-2 гипотеза. Отложено.
- **Unified Seller Partner API v2 (REST)** — новый объединённый API. Машиночитаемой
  спеки не нашли. Отложено. NB: несколько `/v2/*` путей уже присутствуют внутри
  v1-спеки — они помечаются полем `api_version`.

## 3. Контракт авторизации (заземлён на спеку)

```
POST https://public-api-seller.lamoda.ru/jsonrpc/v1/tokens.create
{"jsonrpc":"2.0","id":"<uuid4, ровно 36 символов>","method":"v1.tokens.create",
 "params":{"clientId":"...","clientSecret":"...","grantType":"client_credentials"}}

-> {"jsonrpc":"2.0","id":"...","result":{
     "accessToken":"...","expiresIn":86400,"refreshToken":"...",
     "scope":"admin r_partner r_orders ...","tokenType":"bearer"}}
```

Рефреш: `v1.tokens.update` с `params:{"refreshToken":"..."}` → тот же `TokenResponse`.

Заголовок запросов: `Authorization: Bearer <accessToken>` (из `tokenType: "bearer"`).

**Разрешённое противоречие источников.** Текстовая документация утверждает TTL
15 минут; пример в спеке показывает `expiresIn: 86400`. Хардкодить нельзя ни то,
ни другое — TTL берётся из поля `expiresIn` живого ответа, с упреждающим обновлением
за 60 секунд до истечения. Если поле отсутствует, консервативный дефолт 900 секунд
(меньшее из двух — лишний рефреш дешевле протухшего токена).

## 4. Ключевые архитектурные отличия от WB/Ozon

### 4.1 Ошибка приходит с HTTP 200

Ответ всегда `{jsonrpc, id, result | error}`, где `error` = `{code, message, data}`
с кодами JSON-RPC (`-32600` и др.). HTTP-статус при этом 200.

Обычная проверка `resp.is_success` пропустит ошибку молча — это ровно тот класс
бага, что в marketplace-mcp дал «молча 0 строк» на WB `goods/filter`. Поэтому
распаковка конверта вынесена в отдельный слой `core/jsonrpc.py`, и успех
определяется отсутствием `error`, а не статусом.

### 4.2 Метод дублируется в пути и в теле

`POST /jsonrpc/v1/orders.list` с телом `{"method":"v1.orders.list", ...}`.
Каталог хранит и путь, и имя метода; расхождение между ними — ошибка сборки каталога,
на это есть тест.

### 4.3 Поле `id` строго 36 символов

Спека задаёт `minLength: 36, maxLength: 36`. uuid4 — не стилистический выбор, а требование.

### 4.4 Пагинация `page`, а не cursor

`limit` + `page`, счёт страниц с 1. У WB/Ozon были cursor/last_id/lastchangedate.

## 5. Компоненты

```
core/
  errors.py        канонический конверт ошибки + классификация JSON-RPC кодов
  jsonrpc.py       сборка/разбор конверта JSON-RPC 2.0          <- новое
  registry.py      каталог: EndpointSpec, загрузка, поиск, секции
  safety.py        гейт read/write/destructive + предупреждение о live_verified
  paginate.py      обход страниц, стиль page (limit+page)
  credentials.py   мультикабинетное хранилище ~/.lamoda-mcp/cabinets.json, chmod 600
  client.py        HTTP, авто-рефреш токена, ретраи 429, allowlist хостов
  tools.py         8 мета-тулов, общая реализация
  workflows.py     загрузка рецептов
lamoda_mcp/
  server.py        FastMCP stdio
  endpoints.yaml   каталог 155 методов (генерируется)
  safety_overrides.yaml  ручная курация safety                  <- источник истины
  workflows.yaml   бизнес-рецепты
scripts/
  ingest_lamoda.py    swagger.json -> endpoints.yaml
  audit_safety.py     проверка инварианта «ноль мутаций в read»
  validate_items_path.py  живая валидация (для запуска, когда появятся креды)
```

## 6. Safety

Автоклассификация по суффиксу метода даёт 100 read / 10 write / 1 destructive /
**44 UNKNOWN**. В UNKNOWN попадают именно денежные операции: `set-price`,
`update-price`, `set-prices`, `update-stock`, `update-activation`,
`set-recommended-prices`, `prices-approve`, `order.reset`, `products-add`,
`store`, `approve`, `hide-moderation`, `update-events`.

Поэтому автоклассификация — только черновик. Источник истины — `safety_overrides.yaml`,
заполняемый вручную по описанию метода. Дополнительно перепроверяются все 100
авто-read: в marketplace-mcp эвристика дважды давала ложный read
(`disСount`↔`count`, `status`↔`stat`, `import/info`, `cancel/status`).

Инвариант, проверяемый тестом: **ни одна мутация не помечена read**.

Уровни гейта: `write` → `confirm_write=true`; `destructive` → плюс
`i_understand_this_modifies_data=true`.

### Страховки, которых не было в WB/Ozon

Каталог собран офлайн: ни один запрос ни разу не уходил в живой кабинет Lamoda.
Из-за этого:

1. **`live_verified: false`** на каждом методе. Гейт печатает это в тексте
   предупреждения — агент и пользователь видят, что метод не проверялся боем.
2. **`dry_run=true`** — возвращает точный конверт, который *ушёл бы*, без отправки.
   Позволяет проверить форму `params` до первого реального вызова.
3. **`internal: true`** на 10 методах с тегом `internal api` — они не предназначены
   обычному продавцу.

## 7. Тулы

Восемь мета-тулов, как в marketplace-mcp:
`search_methods`, `describe_method`, `call_method`, `call_raw`, `fetch_all`,
`list_sections`, `get_section`, `check_auth`.

Плюс управление кабинетами (`add_cabinet`, `list_cabinets`, `use_cabinet`)
и `workflows.yaml` с бизнес-рецептами, ссылающимися на реальные `operation_id`
(тест следит, чтобы ссылки не протухли).

## 8. Границы честности

Фиксируется в README и HANDOFF прямым текстом:

- **Твёрдый факт**: перечень 155 методов, их пути, имена, схемы параметров,
  контракт авторизации — всё из официальной спеки Lamoda.
- **Обоснованный вывод, не гарантия**: `items_path`, стиль пагинации и точные формы
  `params` выведены из схем. Урок marketplace-mcp: спека WB обещала `result.items`,
  живой ответ дал `data.listGoods`. Схема — стартовый дефолт, а не истина.
- **Не проверено вообще**: ни одного живого запроса. Нет кредов Lamoda.

`scripts/validate_items_path.py` кладётся готовым под запуск в тот момент, когда
креды появятся; порядок действий описан в HANDOFF.

## 9. Упаковка

`pyproject` с console-script `lamoda-mcp`; `serve.py` с самоподъёмом venv (stdout
остаётся чистым для stdio); `install.py` на 4 клиента (claude-desktop, claude-code,
codex, opencode); install-skill; README в фирменном стиле (охра `#B5491F`,
оранжевый `#D97757`, зелёный `#2D7D4F`); MIT; жёсткий `.gitignore`.

Креды только из env или локального хранилища кабинетов, никогда из кода/аргументов.
Allowlist хостов: `.lamoda.ru` — чтобы уведённый промптом агент не смог отправить
заголовок авторизации на чужой хост через `call_raw`.
