import json
import subprocess

import pytest

import config as C
import resolve as R
import run_agent as A
from conftest import Resp, book, gamma_market

ST0 = {"bankroll": 300.0, "equity": 300.0, "exposure": 0.0, "today": 0.0, "n_open": 0}


def pr(p=0.6, conf=0.8, leaked=False):
    return {"p": p, "conf": conf, "leaked": leaked, "reasoning": "r", "sources": "[]"}


def mkt(mid="1", question="Will X happen?", yes=0.4, url="https://polymarket.com/event/ev"):
    return {"id": mid, "question": question, "yes_price": yes, "url": url,
            "yes_token": "y" + mid, "no_token": "n" + mid, "description": "d", "created": "", "end_date": ""}


# --- costs and sizing -----------------------------------------------------------------------

def test_fee_formula_matches_polymarket_docs():
    assert A.fee_per_share(0.5, 0.07) == pytest.approx(0.07 * 0.25)
    assert A.fee_per_share(0.9, 0.07) == pytest.approx(0.07 * 0.09)
    assert A.fee_per_share(0.5, 0.0) == 0


def test_walk_book_fills_across_levels_and_counts_fee():
    b = book([(0.40, 10), (0.42, 100)], fee_rate=0.07)
    shares, avg, worst, fee = A.walk_book(b, 10)
    assert worst == 0.42
    assert shares * avg == pytest.approx(10)
    assert fee > 0
    assert avg > 0.40


def test_walk_book_none_when_book_too_thin():
    assert A.walk_book(book([(0.4, 1)]), 100) is None


def test_best_bet_picks_side_with_edge():
    y, n = book([(0.40, 1000)]), book([(0.62, 1000)])
    assert A.best_bet(0.55, y, n)[0] == "YES"
    assert A.best_bet(0.25, y, n)[0] == "NO"
    assert A.best_bet(0.45, y, n) is None


def test_best_bet_ignores_empty_book():
    assert A.best_bet(0.9, book([]), book([(0.5, 100)])) is None


def test_shrink_trusts_half_the_disagreement():
    assert A.shrink(0.30, 0.20) == pytest.approx(0.25)
    assert A.shrink(0.20, 0.20) == pytest.approx(0.20)


# --- which forecasts may be traded ------------------------------------------------------------

@pytest.mark.parametrize("p,conf,leaked,expect", [
    (0.60, 0.8, True, "price source"),
    (0.60, 0.5, False, "low confidence"),
    (0.93, 0.8, False, "implausibly large"),      # ETH dip-to-2500: 0.93 vs 0.105 style
    (0.50, 0.8, False, "no edge after shrink"),   # 10pp raw -> 5pp after shrink
    (0.60, 0.8, False, None),
])
def test_skip_reason_filters(p, conf, leaked, expect):
    why = A.skip_reason(pr(p, conf, leaked), mkt(yes=0.40), ST0, set(), {})
    assert (why is None) if expect is None else (expect in why)


def test_skip_reason_one_position_per_event():
    m = mkt()
    assert "event" in A.skip_reason(pr(), m, ST0, {m["url"]}, {})


def test_skip_reason_one_position_per_underlying_asset():
    m = mkt(question="Will the price of Bitcoin be above $80,000 on October 1?")
    assert "CRYPTO" in A.skip_reason(pr(), m, ST0, set(), {"CRYPTO": 1})


def test_skip_reason_max_open_positions():
    assert "max open" in A.skip_reason(pr(), mkt(), dict(ST0, n_open=C.MAX_OPEN_POSITIONS), set(), {})


@pytest.mark.parametrize("q,asset", [
    ("Will Bitcoin dip to $80,000 in September?", "CRYPTO"),
    ("Will Ethereum reach $2,800 in September?", "CRYPTO"),
    ("Will WTI Crude Oil (WTI) hit (HIGH) $95 in September?", "OIL"),
    ("Will Solana reach $300?", "CRYPTO"),
    ("Solana Beach mayor election", None),
    ("Will USA win gold medal in curling?", None),
    ("Golden State Warriors win the title?", None),
])
def test_asset_of(q, asset):
    assert A.asset_of(q) == asset


def test_plan_trade_respects_all_stake_caps():
    y, n = book([(0.40, 10_000)]), book([(0.62, 10_000)])
    plan = A.plan_trade(0.70, 0.40, y, n, ST0, 300)
    assert plan["side"] == "YES"
    assert plan["stake"] <= 300 * C.MAX_POSITION_FRAC + 1e-9
    near_daily_cap = dict(ST0, today=300 * C.MAX_NEW_STAKE_PER_DAY_FRAC - 1)
    assert isinstance(A.plan_trade(0.70, 0.40, y, n, near_daily_cap, 300), str)


def test_plan_trade_rejects_below_min_order_size():
    y, n = book([(0.41, 10_000)], min_size=1_000), book([(0.62, 10_000)])
    assert "min order" in A.plan_trade(0.70, 0.40, y, n, ST0, 300)


# --- model I/O -----------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    '[{"id": "1", "p_yes": 0.3, "confidence": 0.7}]',
    'Here you go:\n```json\n[{"id": "1", "p_yes": 0.3, "confidence": 0.7}]\n```',
    'Note [see sources]. [{"id": "1", "p_yes": 0.3, "confidence": 0.7}]',
])
def test_parse_forecasts_tolerates_wrapping(text):
    out = A.parse_forecasts(text)
    assert out and out[-1]["id"] == "1"


def test_parse_forecasts_raises_on_garbage():
    with pytest.raises(ValueError):
        A.parse_forecasts("I could not complete the research.")


def test_ask_claude_sends_rules_creation_date_time_and_blocks_price_sites(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        out = [{"id": "1", "p_yes": 0.3, "confidence": 0.7, "sources": ["https://reuters.com/x"]},
               {"id": "2", "p_yes": 1.5, "confidence": 0.7},                        # invalid p
               {"id": "3", "p_yes": 0.3, "confidence": 0.7, "sources": ["https://polymarket.com/e"]},
               {"id": "999", "p_yes": 0.3, "confidence": 0.7}]                      # unknown id
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"is_error": False, "result": json.dumps(out)}), "")
    monkeypatch.setattr(A.subprocess, "run", fake_run)
    long_rules = "x" * 3000 + " Price action before this market's creation will not be considered."
    ms = [dict(mkt(str(i)), description=long_rules, created="2026-09-18T16:18") for i in (1, 2, 3)]
    got = A.ask_claude(ms)
    prompt = seen["cmd"][seen["cmd"].index("-p") + 1]
    assert "will not be considered" in prompt           # full rules, not cut at 1200
    assert "2026-09-18T16:18" in prompt                  # creation date reaches the model
    assert "UTC" in prompt                               # current time, not only the date
    assert "WebFetch(domain:polymarket.com)" in seen["cmd"][seen["cmd"].index("--disallowedTools") + 1]
    assert set(got) == {"1", "3"} and got["3"]["leaked"] and not got["1"]["leaked"]


def test_ask_claude_raises_on_limit_error(monkeypatch):
    monkeypatch.setattr(A.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, json.dumps({"is_error": True, "result": "usage limit"}), ""))
    with pytest.raises(RuntimeError):
        A.ask_claude([mkt()])


# --- market selection ---------------------------------------------------------------------------

def gamma_router(markets, closed=()):
    by_id = {m["id"]: m for m in list(markets) + list(closed)}

    def get(url, params=None, timeout=None):
        params = params or {}
        if url.endswith("/markets") and "id" in params:
            m = by_id.get(str(params["id"]))
            want_closed = params.get("closed") == "true"
            return Resp([m] if m and bool(m.get("closed")) == want_closed else [])
        if url.endswith("/markets"):
            off = int(params.get("offset", 0))
            return Resp(list(markets)[off:off + 100])
        raise AssertionError(url)
    return get


def test_fetch_candidates_paginates_and_filters(monkeypatch):
    good = [gamma_market(i, question=f"Will thing {i} happen?") for i in range(150)]
    bad = [gamma_market(1000, liq=10), gamma_market(1001, days=90), gamma_market(1002, yes=0.99),
           gamma_market(1003, question="Is there widespread spread betting?")]
    monkeypatch.setattr(A.requests, "get", gamma_router(good + bad))
    monkeypatch.setattr(C, "MARKETS_PER_RUN", 10_000)
    got = A.fetch_candidates(A.db())
    ids = {m["id"] for m in got}
    assert len(got) == 150                                # page 2 was read (gamma caps pages at 100)
    assert not ids & {"1000", "1001", "1002", "1003"}
    assert all(m["created"] and m["url"].startswith("https://polymarket.com/event/") for m in got)


def test_fetch_candidates_keeps_full_description(monkeypatch):
    m = gamma_market(1, desc="r" * 5000)
    monkeypatch.setattr(A.requests, "get", gamma_router([m]))
    assert len(A.fetch_candidates(A.db())[0]["description"]) == 5000


# --- settlement and reporting --------------------------------------------------------------------

def test_market_state_sees_closed_markets(monkeypatch):
    """Regression: gamma /markets?id= hides closed markets unless closed=true."""
    monkeypatch.setattr(R.requests, "get", gamma_router([], closed=[gamma_market(7, closed=True, outcome=0)]))
    assert R.market_state("7")[0] == 0.0


def test_resolve_all_settles_and_notifies(monkeypatch, isolated):
    con = A.db()
    con.execute("INSERT INTO predictions(ts,market_id,question,p_model,p_market,confidence,version) "
                "VALUES('t','7','Q?',0.2,0.3,0.8,?)", (C.FORECAST_VERSION,))
    con.execute("INSERT INTO positions(ts,market_id,side,token_id,price,stake,shares,mode) "
                "VALUES('t','7','NO','n7',0.7,7,10,'paper')")
    con.execute("INSERT INTO positions(ts,market_id,side,token_id,price,stake,shares,mode) "
                "VALUES('t','8','YES','y8',0.5,5,10,'paper')")
    monkeypatch.setattr(R.requests, "get", gamma_router([gamma_market(8, yes=0.6)],
                                                       closed=[gamma_market(7, closed=True, outcome=0)]))
    marks = R.resolve_all(con)
    assert con.execute("SELECT status, pnl FROM positions WHERE market_id='7'").fetchone() == ("closed", 3.0)
    assert marks == {"8": 0.6}
    assert A.risk_state(con, marks)["equity"] == pytest.approx(300 + 3 + (10 * 0.6 - 5))
    assert any("WON" in t for t, _ in isolated)


def test_report_scores_first_current_version_forecast_only(capsys):
    con = A.db()
    rows = [("a", 0.9, 0.5, 1, C.FORECAST_VERSION, 0), ("a", 0.1, 0.5, 1, C.FORECAST_VERSION, 0),
            ("b", 0.9, 0.5, 1, C.FORECAST_VERSION - 1, 0), ("c", 0.9, 0.5, 1, C.FORECAST_VERSION, 1)]
    for mid, p, q, y, v, lk in rows:
        con.execute("INSERT INTO predictions(ts,market_id,p_model,p_market,confidence,outcome,version,leaked) "
                    "VALUES('t',?,?,?,0.8,?,?,?)", (mid, p, q, y, v, lk))
    R.report(con)
    out = capsys.readouterr().out
    assert "Brier model=0.0100" in out and "n=1 " in out   # only market a, first forecast (0.9)


def test_db_migrates_old_schema():
    import sqlite3
    con = sqlite3.connect(C.DB_PATH)
    con.executescript("CREATE TABLE predictions(id INTEGER PRIMARY KEY, ts TEXT, market_id TEXT, question TEXT,"
                      " p_model REAL, p_market REAL, confidence REAL, reasoning TEXT, sources TEXT, outcome REAL);"
                      "CREATE TABLE positions(id INTEGER PRIMARY KEY, ts TEXT, market_id TEXT, side TEXT,"
                      " token_id TEXT, price REAL, stake REAL, shares REAL, mode TEXT, status TEXT DEFAULT 'open',"
                      " pnl REAL);")
    con.close()
    con = A.db()
    A.db()   # idempotent
    cols = {r[1] for r in con.execute("PRAGMA table_info(positions)")}
    assert {"fee", "order_id", "event"} <= cols


# --- whole run ---------------------------------------------------------------------------------------

def test_main_end_to_end(monkeypatch, isolated):
    ms = [gamma_market(1, question="Will thing 1 happen?", yes=0.40, event="ev1"),
          gamma_market(2, question="Will thing 2 happen?", yes=0.40, event="ev1"),       # sibling of 1
          gamma_market(3, question="Will Bitcoin dip to $80k?", yes=0.40, event="ev3"),
          gamma_market(4, question="Will Ethereum dip to $2,500?", yes=0.40, event="ev4"),  # same group as 3
          gamma_market(5, question="Will thing 5 happen?", yes=0.10, event="ev5")]        # huge gap
    gamma = gamma_router(ms)

    def get(url, params=None, timeout=None):
        if "/book" in url:
            return Resp({"asks": [{"price": "0.41", "size": "5000"}, {"price": "0.43", "size": "5000"}],
                         "min_order_size": "5"})
        if "/fee-rate" in url:
            return Resp({"base_fee": 0})
        return gamma(url, params, timeout)
    monkeypatch.setattr(A.requests, "get", get)
    monkeypatch.setattr(R.requests, "get", get)
    forecasts = [{"id": str(i), "p_yes": 0.70 if i != 5 else 0.90, "confidence": 0.8,
                  "reasoning": "r", "sources": ["https://reuters.com"]} for i in range(1, 6)]
    monkeypatch.setattr(A.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 0, json.dumps({"is_error": False, "result": json.dumps(forecasts)}), ""))
    monkeypatch.setattr(C, "MARKETS_PER_RUN", 10)
    A.main()
    con = A.db()
    held = {r[0] for r in con.execute("SELECT market_id FROM positions")}
    assert con.execute("SELECT COUNT(*) FROM predictions WHERE version=?", (C.FORECAST_VERSION,)).fetchone()[0] == 5
    assert len(held & {"1", "2"}) == 1       # one per event
    assert len(held & {"3", "4"}) == 1       # one per asset
    assert "5" not in held                   # implausible gap
    assert sum(1 for t, _ in isolated if "New YES" in t) == len(held)


def test_main_respects_kill_switch(monkeypatch):
    C.KILL_SWITCH.write_text("stop")
    monkeypatch.setattr(A, "fetch_candidates", lambda con: pytest.fail("must not run"))
    A.main()
