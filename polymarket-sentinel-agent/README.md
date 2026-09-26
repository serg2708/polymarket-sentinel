# PolySentinel Agent

Бот для Polymarket. Каждые 3 часа берёт 12 случайных рынков, которые закрываются в ближайшие 21 день, просит Claude оценить вероятность
исхода **не показывая ему цену**, и если оценка расходится с ценой в стакане больше чем на 7 п.п.
(после комиссии) при уверенности ≥ 0.6 — делает ставку по ¼ Kelly с жёсткими лимитами.

Сейчас работает в **paper** режиме: ставки виртуальные, банк $300, реальные деньги не тратятся.

## Как это устроено

```
systemd timer (каждые 3 ч: 01, 04, 07 … 22 ч)
  └─ run_agent.py
       1. KILL-файл есть? → выход
       2. resolve_all: закрыть разрешившиеся рынки, переоценить открытые позиции
       3. просадка > 30% → создать KILL, выход
       4. выбрать 8 рынков → claude -p (WebSearch/WebFetch) → p_yes, confidence
       5. для каждого: стакан CLOB → перевес после комиссии → размер ставки → позиция
  └─ всё пишется в agent.db (SQLite)

weekly timer (пн 09:13) → resolve.py → reports/report-ДАТА.txt
```

## Уведомления в Telegram

Приходят через бота основного проекта (`TELEGRAM_BOT_TOKEN`, `ADMIN_CHAT_ID` из `../.env`):

| Событие | Сообщение |
|---|---|
| Новая сделка | 🟢 сторона, сумма, цена, прогноз vs рынок, обоснование модели |
| Рынок разрешился | ✅ WON / 🔻 LOST и PnL |
| Стоп по просадке | 🛑 создан KILL, агент остановлен |
| Ошибка Claude (лимиты и т.п.) | ⚠️ запуск пропущен |
| Падение скрипта | ❌ текст ошибки |
| Понедельник 09:13 | 📊 недельный отчёт |
| Понедельник 09:13 | 🧪 бэктест LLM-сигналов основного бота (tail_risk, soft_edge, news_divergence) |

Запуски без сделок молчат. С 22:00 до 08:00 сообщения приходят без звука.

## Повседневное использование

Все команды — из папки агента:
```bash
cd ~/dev/polymarket-sentinel/polymarket-sentinel-agent
```

| Что | Команда |
|---|---|
| Отчёт: есть ли перевес, PnL | `.venv/bin/python resolve.py` |
| Все ставки с текущими ценами | `.venv/bin/python resolve.py --positions` |
| Бэктест сигналов основного бота | `.venv/bin/python backtest_signals.py` |
| Еженедельные отчёты | `ls reports/` , `cat reports/report-*.txt` |
| Логи в реальном времени | `journalctl --user -u polysentinel-agent -f` |
| Логи последних запусков | `journalctl --user -u polysentinel-agent -n 100 --no-pager` |
| Когда следующий запуск | `systemctl --user list-timers 'polysentinel*'` |
| Запустить прямо сейчас | `systemctl --user start polysentinel-agent` |
| **Аварийная остановка** | `touch KILL` |
| Возобновить после остановки | `rm KILL` |
| Выключить таймер совсем | `systemctl --user disable --now polysentinel-agent.timer` |

### Посмотреть прогнозы и позиции

```bash
sqlite3 agent.db "SELECT ts, question, p_model, p_market, confidence FROM predictions ORDER BY id DESC LIMIT 20"
sqlite3 agent.db "SELECT ts, market_id, side, price, stake, status, pnl FROM positions ORDER BY id DESC"
```
(`sudo apt install sqlite3`, если нет.)

## Как читать отчёт

```
All         : n=42  Brier model=0.1810  market=0.1950  z=-2.30  -> model beats market (z<-2)
Confident   : n=18  ...
[paper] closed=9 pnl=$14.20 (fees $1.10) ROI=+8.1%
```

- **Brier** — ошибка прогноза, меньше = лучше. Сравнивается модель и цена рынка в момент прогноза.
- **z** — насколько разница неслучайна. Только **z < −2** значит, что модель реально точнее рынка.
- **n < 30** — данных мало, выводы делать рано. Реалистично 3–5 недель.
- **Confident** — только прогнозы с уверенностью ≥ 0.6, именно на них бот ставит.
- **ROI** — доходность paper-сделок с учётом комиссий.
- **Версия прогнозов** (`FORECAST_VERSION` в `config.py`): отчёт учитывает только текущую версию.
  Меняете промпт или входные данные модели — увеличьте версию, иначе старые прогнозы смешаются с новыми.
  v1 (до 25.09.2026) — модель видела обрезанные правила рынка; v2 — полные.

## Когда переходить на реальные деньги

Все условия одновременно:
1. `n ≥ 30` и вердикт **model beats market** (хотя бы в строке `Confident`).
2. Paper ROI положительный на ≥ 15 закрытых сделках.
3. Первый live-ордер проверен руками на $5 (код live-ордеров не тестировался на реальном аккаунте).

Если вердикт **NO EDGE** — не включать live. Это не баг, это ответ: модель не лучше рынка.

### Включение live

1. Секреты в `~/.config/polysentinel/secrets.env` (`chmod 600`):
   ```
   POLY_PK=0x...          # приватный ключ кошелька
   POLY_FUNDER=0x...      # адрес прокси-кошелька Polymarket (где лежит USDC)
   POLY_SIG_TYPE=1        # 1 = email/Magic, 2 = браузерный кошелёк, 0 = EOA
   ```
2. В `polysentinel-agent.service`: `AGENT_MODE=live`, `AGENT_BANKROLL=` — сколько реально готов потерять.
3. Применить:
   ```bash
   cp polysentinel-agent.service ~/.config/systemd/user/ && systemctl --user daemon-reload
   ```

Paper и live позиции в базе разделены (`mode`), лимиты считаются отдельно.

## Настройки (`config.py`)

| Параметр | Сейчас | Смысл |
|---|---|---|
| `MIN_EDGE` | 0.07 | мин. перевес после комиссии для ставки |
| `MIN_CONFIDENCE` | 0.6 | мин. уверенность модели |
| `KELLY_FRACTION` | 0.25 | доля Kelly |
| `MAX_POSITION_FRAC` | 0.05 | макс. 5% банка на позицию |
| `MAX_TOTAL_EXPOSURE_FRAC` | 0.40 | макс. 40% банка в открытых позициях |
| `MAX_NEW_STAKE_PER_DAY_FRAC` | 0.15 | макс. 15% банка новых ставок в день |
| `MAX_DRAWDOWN` | 0.30 | просадка → автоматический KILL |
| — | 1 | не больше одной открытой позиции на событие (разные даты одного вопроса = одна ставка) |
| `MARKETS_PER_RUN` | 12 | рынков за запуск (расход лимитов Claude) |
| `MAX_DAYS_TO_END` | 21 | только рынки, которые закроются в ближайшие 21 день (быстрый итог) |

Не ослабляйте `MIN_EDGE`/`MIN_CONFIDENCE`, чтобы «было больше сделок», — это самый быстрый способ
потерять деньги. Меняйте параметры только по данным из отчёта.

## Установка с нуля

```bash
cd ~/dev/polymarket-sentinel/polymarket-sentinel-agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp polysentinel-*.service polysentinel-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now polysentinel-agent.timer polysentinel-report.timer
loginctl enable-linger $USER     # чтобы работало, когда вы не залогинены
```
Нужен установленный и залогиненный Claude Code CLI (`~/.local/bin/claude`).

## Если что-то не так

- **`claude failed: ... limits`** — кончились лимиты подписки; запуск пропускается, следующий пройдёт сам.
- **`candidates: 0`** — все подходящие рынки уже прогнозировались за 24 ч; нормально.
- **`skip ...: forecast cited a price source`** — модель подсмотрела цену; прогноз не используется.
- **Файл `KILL` появился сам** — сработал стоп по просадке. Разберитесь в причинах, прежде чем удалять.
