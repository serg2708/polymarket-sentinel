# PolySentinel — торговый бот для Polymarket

Система мониторинга рынков предсказаний. Находит расхождения в ценах между платформами, аномалии ликвидности и информационные сигналы. Отправляет алерты в Telegram.

---

## Как работает бот

```
1. ingest — собирает данные каждые 5–300 секунд
   ├── Polymarket WebSocket → цены в реальном времени → TimescaleDB
   ├── Kalshi REST poll → цены каждые 30 сек (если API ключ есть)
   ├── NewsAPI + RSS (BBC, NYT, Al Jazeera и др.) → статьи в Redis
   └── Gamma API → список рынков + метаданные каждые 5 мин

2. detectors — сканирует DB/Redis каждые 5 секунд
   ├── Арбитраж: YES+NO < $1.00 → мгновенный алерт
   ├── Ликвидность: спред, дисбаланс книги, price spike
   ├── Tail Risk: рынок 3–35% + LLM говорит "выше" → алерт (раз в 1ч)
   └── News Divergence: LLM скорит новости, EMA сентимента vs цена (раз в 2 мин)

3. matcher — сопоставляет рынки между платформами
   └── Polymarket ↔ Kalshi / Manifold через LLM (Claude или Ollama)

4. bot — доставляет алерты
   ├── Читает очередь Redis
   ├── Проверяет дедупликацию, мьюты, quiet hours (22:00–08:00 CET)
   └── Отправляет в Telegram с кнопками
```

---

## Как используется Claude (и Ollama)

Бот использует LLM в трёх местах. Модель выбирается по настройке `OLLAMA_PRIMARY`:
- `OLLAMA_PRIMARY=true` → сначала Ollama (локально, бесплатно), Claude как fallback
- `OLLAMA_PRIMARY=false` → сначала Claude API, Ollama как fallback

**Рекомендуемая модель:** `qwen2.5:7b-instruct-q8_0` (8 GB VRAM, отличное качество JSON)

### 1. News Divergence — скоринг новостей

**Файл:** `packages/detectors/news_divergence.py`

Каждые 2 минуты для каждого отслеживаемого рынка:
1. Берёт последние 5–10 статей из Redis (загружены ingest-ом)
2. Отправляет каждую статью в LLM с промптом:
   > "Рынок: {вопрос}, текущая цена YES: {цена}. Эта новость повышает (R), понижает (L) или не влияет (U) на P(YES)? Укажи confidence 0.0–1.0"
3. LLM возвращает JSON: `{"direction": "R", "confidence": 0.85, "reason": "..."}`
4. Считает EMA сентимента по всем статьям (alpha=0.3)
5. Если `|EMA| > 0.4` и цена рынка не соответствует → алерт

### 2. Tail Risk — оценка недооценённых событий

**Файл:** `packages/ingest/llm_prior.py`

Раз в 1 час для рынков с ценой **3–35%** (объём >20k):
1. Передаёт LLM текст вопроса + свежие заголовки новостей
2. LLM оценивает P(YES) с confidence и объяснением
3. Если LLM даёт оценку значительно выше рыночной (gap ≥ 3 пп, множитель ≥ 1.25×, confidence ≥ 0.65) → алерт

Покрывает: геополитику (перевороты, эскалации), мульти-outcome ивенты (Eurovision, Оскар), регуляторные сюрпризы, крипто.

**Важно:** Tail Risk срабатывает только при наличии свежих новостей (NewsAPI key нужен). Без новостей сигналов не будет.

### 3. Market Matching — сопоставление рынков

**Файл:** `packages/matcher/`

Автоматически ищет одинаковые вопросы на разных платформах:
1. Берёт рынки с Polymarket и Kalshi/Manifold
2. Отправляет пары в LLM: "Это один и тот же вопрос? Уверенность 0–100%"
3. Пары с confidence ≥ 85% → `approved_by = 'auto'` (активны автоматически)
4. Пары с confidence < 85% → `approved_by = 'pending'` (нужна ручная проверка)

**Использует claude-haiku** или Ollama, запускается каждые 30 мин (Kalshi) и 6 часов (Manifold).

### Текущий статус (что отключено)

```
✅ включено:   news_divergence, tail_risk, auto-matching
❌ отключено:  soft_edge (API вызовы Metaculus/Manifold/Claude)
❌ отключено:  llm_prior standalone scan (4h cycle)
```

Причина: экономия API токенов. Tail risk и news divergence уже используют LLM эффективно.

---

## Архитектура

```
Polymarket WS/REST ──┐
Manifold API         ├── ingest ──► DB (TimescaleDB) + Redis
Kalshi API (опц.)    ┘                       │
NewsAPI / RSS (BBC, NYT,                     │
  Al Jazeera, FP...)      ┌──────────────────┤
                           │                 │
                        detectors         matcher
                           │                 │
                     алерты в Redis   market_matches в DB
                           │
                          bot ──► Telegram
```

### Сервисы

| Сервис | Порт | Назначение |
|---|---|---|
| `ingest` | 8000 | Цены с Polymarket (WS + REST), Kalshi, новости RSS/NewsAPI |
| `detectors` | 8001 | Обнаружение сигналов каждые 5 сек |
| `bot` | 8002 | Отправка алертов в Telegram, команды |
| `matcher` | 8003 | Поиск пар рынков между платформами |
| `db` | 5432 | TimescaleDB — цены, рынки, алерты |
| `redis` | 6379 | Очередь алертов, дедупликация, кеш |
| ~~`prometheus`~~ | 9090 | Отключён в `docker-compose.yml` — не нужен для работы бота |
| ~~`grafana`~~ | 3000 | Отключён в `docker-compose.yml` — не нужен для работы бота |

> Prometheus и Grafana закомментированы в `docker-compose.yml` для снижения нагрузки на ПК. Раскомментировать при необходимости мониторинга метрик.

---

## Запуск

```bash
# Первый раз
cp .env.example .env   # заполни токены
cd polysentinel
docker compose build
docker compose up -d

# Проверить статус
docker compose ps
docker compose logs -f detectors

# Пересобрать после изменения кода (restart НЕ работает)
docker build --no-cache -t polysentinel-detectors:latest \
  -f packages/detectors/Dockerfile .
docker compose up -d --force-recreate detectors
```

> ⚠️ `docker compose restart` не подхватывает изменения кода — всегда нужен `build --no-cache` + `up --force-recreate`.

### Если контейнеры упали (db/redis exited 255)

Происходит при переходе ПК в сон — Docker Desktop VM убивается. Восстановление:

```bash
docker compose up -d
```

Для постоянного решения: Docker Desktop → Settings → Resources → снять галку **"Enable Resource Saver"**. Это главная причина крашей — VM паузится при простое.

### Переменные окружения (`.env`)

```env
# Telegram
TELEGRAM_BOT_TOKEN=...
ADMIN_CHAT_ID=...              # твой chat_id

# Ollama (основная LLM — бесплатно, локально)
OLLAMA_BASE_URL=http://192.168.65.2:11434   # адрес хоста внутри Docker Desktop VM
OLLAMA_MODEL=qwen2.5:7b-instruct-q8_0       # рекомендуется для 8 GB VRAM
OLLAMA_PRIMARY=true

# Claude API (опционально — fallback если Ollama недоступна)
ANTHROPIC_API_KEY=                           # оставить пустым если не нужен

# Kalshi (опционально — нужен для cross-platform arb)
KALSHI_API_KEY_ID=
KALSHI_PRIVATE_KEY_PATH=

# Metaculus (опционально — community predictions)
METACULUS_API_TOKEN=...

# News
NEWSAPI_KEY=...

# Пороги алертов
ARB_MIN_EDGE_BPS=100          # минимальный кросс-платформ арбитраж (1 пп)
SOFT_EDGE_MIN_BPS=500         # минимальный relative edge для SOFT EDGE (5%)
SOFT_EDGE_MIN_PP=5.0          # минимальный абсолютный gap в пп
LIQUIDITY_SPREAD_THRESHOLD=0.15
LIQUIDITY_MIN_MID=0.10        # игнорировать рынки дешевле 10¢ или дороже 90¢

# Ingest
TOP_MARKETS_BY_VOLUME=500
MAX_MARKETS_PER_EVENT=8       # не более N рынков из одного события

# Дополнительные рынки: всегда отслеживаются независимо от объёма
# Добавить market_id через запятую. После изменения — только restart ingest (не rebuild).
SUPPLEMENTAL_MARKET_IDS=701539,701540,...   # ETH EOY 2026
```

> После изменения `SUPPLEMENTAL_MARKET_IDS` достаточно `docker compose up -d ingest` — пересборка образа не нужна.

---

## Источники сигналов

| Источник | Надёжность | Комментарий |
|---|---|---|
| **Kalshi** (кросс-платформ арб) | ★★★★★ | Реальные деньги, механический профит. Требует API ключ |
| **Intra-market arb** | ★★★★★ | YES+NO внутри Polymarket < $1. Без доп. ключей |
| **Metaculus** | ★★★★☆ | Community CP, реальные форкастеры. Требует API токен выше free |
| **PredictIt** | ★★★☆☆ | Реальные деньги. −10% комиссия на прибыль. Гео-блок вне США |
| **Tail Risk (LLM + News)** | ★★★☆☆ | Недооценённые события 3–35%. Работает на Ollama, для срабатывания желательны свежие новости |
| **News Divergence** | ★★★☆☆ | Сентимент новостей vs рыночная цена. Хорошо работает на макро/крипто |
| **Manifold** | ★★☆☆☆ | Play-money (Mana). Частый recency/partisan bias |
| **LLM Prior (Ollama/Claude)** | ★★☆☆☆ | Оценка P(YES) через LLM. Только вспомогательный сигнал |

---

## Типы алертов

### ⚡ CROSS-PLATFORM ARB

**Что это:** Гарантированный арбитраж — купить YES на Polymarket + NO на Kalshi суммарно дешевле $1. Прибыль при любом исходе.

**Требует:** Kalshi API ключ

```
⚡ CROSS-PLATFORM ARB  +150 bps (1.5%)
Will OKC Thunder win the 2026 NBA Finals?

BUY YES @ Polymarket  avg 49.1¢
BUY NO  @ Kalshi      avg 50.2¢

For 100 contracts:
  Outlay → $99.30   Payout → $100.00
  Profit: +$0.70
```

**Действие:** Купить обе стороны одновременно. Калши берёт комиссию ~1% — уже учтена в расчёте.

---

### 🔄 INTRA-MARKET ARB

**Что это:** YES + NO внутри одного рынка Polymarket суммарно стоят меньше $1.

```
🔄 INTRA-MARKET ARB  +80 bps (0.8%)
Will Argentina win the 2026 FIFA World Cup?

YES+NO bundle cheaper than $1:
  YES 48.8¢  +  NO 50.7¢

For 100 contracts:
  Outlay → $99.50   Payout → $100.00
  Profit: +$0.50
```

**Действие:** Купить и YES и NO токены одновременно. Пара всегда даёт $1 при резолюции.

---

### 🎯 TAIL RISK — UNDERPRICED

**Что это:** Рынок с вероятностью 5–25% недооценён согласно свежим новостям. Claude анализирует последние заголовки и выдаёт сигнал только при наличии конкретных фактов (confidence ≥ 0.70). Запускается раз в 2 часа.

```
🎯 TAIL RISK — UNDERPRICED
Montreal Canadiens win the 2026 NHL Stanley Cup

📈 BUY YES
Market: 7.5¢  Claude: 18.0%  (confidence 85%)
Gap: +10.5 pp  EV per $1: +1.40
Kelly ½: 3.2% of bankroll

💡 Montreal Canadiens have taken a series lead (3-2) against Tampa Bay
🤖 Verify with news before acting
```

**Поля:**
- `confidence` — качество новостного покрытия: ≥0.80 = свежие конкретные факты, 0.70–0.79 = умеренные данные
- `Kelly ½` — консервативный размер ставки (½ от полного Kelly, т.к. оценка Claude, не механический арб)
- `⚡ Order flow confirms` — появляется, если за последний час был Price Spike или Book Imbalance по тому же рынку

**Когда торговать:** gap ≥ 10 пп + confidence ≥ 0.80 + проверить новости вручную.

---

### 📊 / 🤖 / 🏛️ SOFT EDGE

**Что это:** Цена на Polymarket отличается от вероятности на внешней платформе или оценки Claude.

```
📊 SOFT EDGE — MANIFOLD
Will the Republicans win the 2028 US Presidential Election?

📈 Under-priced → BUY YES
Manifold: 59.1%  Market: 38.5¢
Gap: +20.6 pp  EV per $1: +0.536
Kelly ¼: 4.2% of bankroll
⚠️ Manifold = play money (Mana) — Kelly already halved
✅ Rules align
```

**Поля:**
- `Gap: ±X pp` — разница в процентных пунктах
- `EV per $1` — ожидаемая прибыль на каждый вложенный доллар
- `Kelly ¼: X%` — рекомендуемый размер ставки (¼ от полного Kelly)

**Когда торговать по источнику:**

| Источник | Действие |
|---|---|
| Metaculus CP (🎯) | Торговать при gap ≥ 5 пп, Kelly ≥ 0.5% |
| PredictIt (🏛️) | Торговать при gap ≥ 5 пп (учти −10% fee) |
| Manifold (📊) | Проверить вручную, малая ставка (½ от Kelly) |
| LLM Prior (🤖) | Только информация, не торговать автоматически |

**Размер ставки:** `Kelly ¼ (%) × депозит`. При $1000 и Kelly 2.1% → ставить $21.

---

### ⚖️ BOOK IMBALANCE

**Что это:** Сильный дисбаланс на верхнем уровне книги заявок.

```
⚖️ BOOK IMBALANCE · No
Will the Carolina Hurricanes win the 2026 NHL Stanley Cup?

📉 Bearish — buying NO  10.6×
YES: 38.5¢  Bid: $803   Ask: $8610
```

**Интерпретация:**
- `Bid-heavy` → давление на покупку → цена скорее вырастет
- `Ask-heavy` → давление на продажу → цена скорее упадёт

> Сам по себе не торгуется. Используй как подтверждение к SOFT EDGE или PRICE SPIKE. Ratio >10× с объёмом >$1000 — сильный сигнал.

---

### 🚀 PRICE SPIKE

**Что это:** Цена отклонилась от скользящего среднего на ≥3σ.

```
🚀 PRICE SPIKE · Yes
Will Bitcoin hit $1M before GTA VI?

Z-score: +3.45σ  (58 ticks)
Now: 52.3¢   Mean: 49.1¢   ±0.93¢
```

**Интерпретация:** Резкое движение — кто-то действует с информацией. Без контекста — часто шум. Смотри вместе с Book Imbalance и новостями.

---

### 📐 WIDE SPREAD

**Что это:** Спред между bid и ask >15% от midpoint — рынок неликвиден.

```
📐 WIDE SPREAD · Yes
Will France win the 2026 FIFA World Cup?

Spread: 8.0¢  (18.2% of mid)
Bid: 35.0¢   Ask: 43.0¢
```

**Действие:** Не торговать — при входе сразу теряешь половину спреда.

---

### 🔢 SUM DEVIATION

**Что это:** YES + NO мид отличаются от $1 более чем на порог.

- `Sum < 1` (📉) → купить YES+NO bundle (intra-market arb)
- `Sum > 1` (📈) → рынок перегрет

---

### 📰 NEWS DIVERGENCE

**Что это:** Сентимент свежих новостей расходится с рыночной ценой. Детектор читает последние статьи по рынку, скорит каждую (−1..+1), считает EMA сентимента и сравнивает с market_p.

```
📰 NEWS DIVERGENCE  (conf 85%)
Will no Fed rate cuts happen in 2026?

📉 Bearish — news more negative than price implies → consider BUY NO
Market: 50.0¢  Sentiment EMA: -0.518 (negative)
Articles scored: 10

💡 Higher inflation (3.5% core) reduces probability of zero cuts...
  • Core Inflation Rate Jumps to Its Highest in Years Thanks to Iran War
🤖 Verify news before acting
```

**Поля:**
- `Sentiment EMA` — экспоненциальная скользящая средняя по оценкам новостей (отрицательный = медвежий тон)
- `conf X%` — уверенность сигнала (≥80% = сильный сигнал)
- `Bearish / Bullish` — направление: Bearish → рынок перегрет (BUY NO), Bullish → рынок недооценён (BUY YES)

**Когда торговать:**
- Уверенность ≥ 80%
- Sentiment EMA < −0.40 (Bearish) или > +0.40 (Bullish)
- Совпадает с другим сигналом (Soft Edge или Tail Risk)
- Проверь вручную: новости актуальны, не устарели

> 🤖 Источник новостей — NewsAPI + RSS. Могут попасть нерелевантные статьи. Всегда проверяй `reason` и заголовки перед ставкой.

---

## Пары рынков (market_matches)

### Ручные пары (`manual_map.yaml`)

```yaml
markets:
  - key: my_market_key
    polymarket: "123456"             # market_id из БД
    manifold: "some-manifold-slug"   # slug из URL manifold.markets/...
    # manifold: "slug#AnswerText"    # для multi-choice рынков
    metaculus: 12345                 # ID вопроса
    predictit: "market_id/contract_id"
    notes: "Описание различий в правилах"

  # Standalone запись (только polymarket) — попадает в /list и news_divergence,
  # но не даёт cross-platform soft_edge (нет внешней пары для сравнения)
  - key: eth_dip_1500_dec2026
    polymarket: "701552"
    notes: "ETH dip to $1,500 by Dec 31, 2026. (844k vol)"
```

**Покрытые рынки:**
- Геополитика: Иран, Украина, Китай-Тайвань, Израиль-Саудовская Аравия
- Макро: ФРС (ставки 2026), рецессия США
- Крипто: **16 ETH EOY 2026** (`eth_reach_3500…10000`, `eth_dip_800…2500`), BTC $150k
- Спорт: NBA Finals 2026 (OKC), FIFA World Cup 2026 (Франция, Аргентина, Бразилия)
- AI: OpenAI IPO, лучшая модель AI (Anthropic vs OpenAI vs xAI)
- Политика США: выборы 2028 (Трамп, Вэнс, Ньюсом)

После изменения YAML — обязательно пересобрать:

```bash
docker compose build matcher
docker compose up -d matcher
```

> ⚠️ `docker compose restart matcher` **не поможет** — YAML запекается в образ при сборке.

### Авто-матчинг

| Пара | Интервал | LLM |
|---|---|---|
| Polymarket ↔ Kalshi | каждые 30 мин | Claude (fallback: Ollama) |
| Polymarket ↔ Manifold | каждые 6 часов | Claude (fallback: Ollama) |

Пары с уверенностью ≥85% активируются автоматически (`approved_by = 'auto'`).
Пары с уверенностью <85% → `approved_by = 'pending'` (нужна ручная проверка).

---

## Команды бота в Telegram

| Команда | Описание |
|---|---|
| `/start` | Начало работы |
| `/status` | Состояние сервисов + последний алерт |
| `/list` | Все отслеживаемые рынки (разбивается на страницы если > ~70) |
| `/calibration` | Точность модели (Brier score) |
| `/explain <alert_id>` | Детальный разбор алерта по ID |
| `/watch <slug>` | Добавить рынок по slug |
| `/pause <2h>` | Заглушить все алерты на время |
| `/resume` | Возобновить алерты |
| `/threshold arb <bps>` | Изменить порог арбитража |
| `/threshold soft <bps>` | Изменить порог soft edge |

`group_key` виден в каждом алерте или в БД: `SELECT DISTINCT group_key FROM alerts`.

Кнопки под каждым алертом:
- **Polymarket ↗** — открыть рынок (где применимо)
- **Mute 1h** / **Mute forever** — заглушить этот рынок

---

## Дедупликация алертов

Повторные алерты одного типа по одному рынку подавляются. Ключ дедупа для фундаментальных сигналов — только `kind:group_key` (без размера edge), чтобы ±50 bps дрейф не создавал новые дубли.

| Тип | Минимальный интервал | Edge в ключе? |
|---|---|---|
| `arb_xplatform`, `arb_intramarket` | 30 мин | ✅ (50 bps bucket) |
| `price_spike`, `sum_deviation` | 30 мин | ✅ (50 bps bucket) |
| `book_imbalance` | 1 час | ❌ |
| `wide_spread` | 2 часа | ❌ |
| `soft_edge_predictit` | 2 часа | ❌ |
| `soft_edge_manifold`, `soft_edge_metaculus` | 4 часа | ❌ |
| `soft_edge_llm_prior` | 6 часов | ❌ |
| `tail_risk` | 6 часов | ❌ |
| `news_divergence` | 12 часов | ❌ |

---

## Мониторинг

```bash
# Логи в реальном времени
docker compose logs -f detectors
docker compose logs -f bot

# Все алерты за последний час
docker exec polysentinel-db-1 psql -U postgres polysentinel \
  -c "SELECT kind, group_key, edge_bps, payload->>'title', ts
      FROM alerts ORDER BY ts DESC LIMIT 20;"

# Активные пары рынков
docker exec polysentinel-db-1 psql -U postgres polysentinel \
  -c "SELECT group_key, source, source_id, approved_by
      FROM market_matches ORDER BY group_key, source;"

# Потребление памяти контейнеров
docker stats --no-stream

# Кеш LLM prior
docker exec polysentinel-redis-1 redis-cli KEYS "llm_prior:*"

```

### Лимиты памяти контейнеров

Docker Desktop VM рекомендуется ограничить **8–16 GB** (Settings → Resources → Memory).

| Контейнер | Реальный пик | Лимит |
|---|---|---|
| `db` | ~400MB | 2GB |
| `redis` | 512MB (self-cap) | 768MB |
| `ingest` | ~150MB | 768MB |
| `detectors` | ~120MB | 768MB |
| `bot` | ~200MB | 512MB |
| `matcher` | ~2.4GB (при запуске embedding) | 8GB |
| **Итого реально** | **~3.6GB** | — |

---

## Базовые правила торговли

1. **Арб — торговать всегда** при edge ≥ 1 пп, нет зависимости от прогноза
2. **SOFT EDGE торговать только если** gap ≥ 5 пп, Kelly ≥ 0.5%, mid между 15¢ и 85¢
3. **TAIL RISK торговать только если** gap ≥ 10 пп + confidence ≥ 0.80 + проверить новости вручную
4. **NEWS DIVERGENCE торговать только если** conf ≥ 80% + sentiment EMA > 0.40 + совпадает с другим сигналом
5. **Проверяй notes в YAML** — разные правила резолюции делают сравнение бессмысленным
6. **Manifold на политике далёкого горизонта (>1 год) — не торговать**: recency bias и отсутствие финансового стимула
7. **LLM Prior — только информация**, никогда не единственное основание для ставки
8. **Book Imbalance и Price Spike** — не торговые сигналы сами по себе, только подтверждение
9. **Kelly ¼** — консервативная оценка. Никогда не ставь полный Kelly на play-money источник
10. **Мути шумные рынки**: `/mute <group_key> forever`
11. **Позиционные ставки (tail risk, ETH)** — горизонт недели/месяцы. Цена двигается при прохождении раунда плей-офф, выходе ключевых новостей, или при резолюции. Не продавать на шуме ±2%.

### Рынки "before GTA VI" (Jesus, Russia-Ukraine ceasefire и др.)

Это косвенная ставка на дату выхода GTA VI (~осень 2026). NO означает "GTA VI выйдет раньше, чем произойдёт X". Если выход GTA VI подтверждён → все NO позиции близки к резолюции. YES = вечная задержка.
