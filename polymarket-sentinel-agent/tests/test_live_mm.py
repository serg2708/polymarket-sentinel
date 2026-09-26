import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mm"))
import live_mm as L  # noqa: E402

FAR = 4_000_000_000
M = {"cond": "c1", "question": "Will X?", "yes": "Y", "no": "N", "min_size": 20, "v": 4.5, "tick": 0.01,
     "neg_risk": False, "end_ts": FAR, "event_ts": None, "accepting": True}
BOOK = ([(0.49, 300)], [(0.51, 300)])


def test_targets_two_sided_quote_as_two_bids():
    want, why = L.targets(M, *BOOK, 0, 0, 1000, capital=49)
    assert why is None
    assert want[("Y", "BUY")][0] == pytest.approx(0.49) and want[("N", "BUY")][0] == pytest.approx(0.49)
    assert want[("Y", "BUY")][1] == pytest.approx(50)                                     # 49 / (1 - 0.02)


@pytest.mark.parametrize("m,book,why", [
    (dict(M, event_ts=1000 + 60), BOOK, "event imminent"),
    (dict(M, end_ts=1000 + 60), BOOK, "market ending"),
    (M, ([(0.04, 300)], [(0.06, 300)]), "mid outside"),
    (M, ([], [(0.5, 10)]), "no book"),
])
def test_targets_flat_when_unsafe(m, book, why):
    want, reason = L.targets(m, *book, 0, 0, 1000)
    assert want == {} and why in reason


def test_targets_sells_held_yes_instead_of_buying_no():
    """Pilot 1: after a YES fill the bot kept trying to BUY NO with cash it no longer had."""
    want, _ = L.targets(M, *BOOK, 45.92, 0, 1000, cash=12.35, capital=45)
    assert want[("Y", "SELL")] == (pytest.approx(0.51), 45.92)                           # offer the inventory
    assert ("N", "BUY") not in want
    assert ("Y", "BUY") not in want                                                      # at the inventory cap


def test_targets_sells_held_no_on_the_bid_side():
    want, _ = L.targets(M, *BOOK, 0, 30, 1000, cash=100, capital=45)
    assert want[("N", "SELL")] == (pytest.approx(0.51), 30)                              # 1 - bid
    assert ("Y", "BUY") not in want and ("N", "BUY") in want


def test_targets_buys_sized_to_cash():
    want, _ = L.targets(M, *BOOK, 0, 0, 1000, cash=10, capital=45)
    spent = sum(p * sz for (tok, side), (p, sz) in want.items() if side == "BUY")
    assert spent <= 10 * L.CASH_BUFFER + 1e-9
    none, why = L.targets(M, *BOOK, 0, 0, 1000, cash=1, capital=45)
    assert none == {} and "affordable" in why


def test_targets_inventory_cap_stops_the_heavy_side():
    size = L.targets(M, *BOOK, 0, 0, 1000, capital=49)[0][("Y", "BUY")][1]
    long_yes = L.targets(M, *BOOK, size, 0, 1000, capital=49)[0]
    assert ("Y", "BUY") not in long_yes and ("Y", "SELL") in long_yes


def test_diff_keeps_order_at_right_price_for_queue_priority():
    cur = [{"id": "a", "token": "Y", "side": "BUY", "price": 0.49, "size": 50},
           {"id": "b", "token": "N", "side": "BUY", "price": 0.47, "size": 50},        # stale price
           {"id": "c", "token": "Y", "side": "BUY", "price": 0.49, "size": 50},        # duplicate
           {"id": "d", "token": "Y", "side": "SELL", "price": 0.49, "size": 50}]       # wrong side
    cancel, place = L.diff_orders(cur, {("Y", "BUY"): (0.49, 50), ("N", "BUY"): (0.49, 50)}, 0.01)
    assert set(cancel) == {"b", "c", "d"} and place == [("N", "BUY", 0.49, 50)]


def test_diff_replaces_mostly_filled_order():
    cancel, place = L.diff_orders([{"id": "a", "token": "Y", "side": "BUY", "price": 0.49, "size": 10}],
                                  {("Y", "BUY"): (0.49, 50)}, 0.01)
    assert cancel == ["a"] and place == [("Y", "BUY", 0.49, 50)]


@pytest.mark.parametrize("q,risky", [
    ('Will "BbY WOW - KAROL G" be the #2 song this week?', True),
    ("Will the highest temperature in Seoul be 26°C?", True),
    ("Will MrBeast Gaming's next video get between 35 and 40 million views?", True),
    ("Will Bitcoin reach $90,000 in September?", True),
    ("Will “Safe” by Cardi B win Best Hip-Hop at the 2026 VMAs?", False),
    ("Xi Jinping out before 2027?", False),
    ("Will Sonny Gray lead MLB in pitcher wins for the 2026 season?", True),
])
def test_info_risk_filter(q, risky):
    assert bool(L.INFO_RISK.search(q)) == risky


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "DB_PATH", tmp_path / "live.db")
    monkeypatch.setattr(L, "KILL", tmp_path / "KILL")
    monkeypatch.setattr(L, "market_info", lambda cond: dict(M, cond=cond))
    monkeypatch.setattr(L.SH, "book", lambda token: BOOK)
    sent = []
    monkeypatch.setattr(L, "send", lambda text, url=None, tag=None: sent.append(text))
    r = L.Runner(L.DryRunExchange(cash=100), ["c1"], live=False)
    r.sent = sent
    return r


def test_quote_market_places_then_keeps(runner):
    runner.quote_market(runner.markets[0], 1000)
    assert len(runner.ex.orders) == 2
    ids = set(runner.ex.orders)
    runner.quote_market(runner.markets[0], 1015)
    assert set(runner.ex.orders) == ids                       # unchanged book -> orders left alone


def test_daily_loss_kills_and_cancels(runner):
    runner.quote_market(runner.markets[0], 1000)
    runner.check_risk()                                       # day start equity = 100
    runner.ex._cash = 100 - L.MAX_DAILY_LOSS - 1
    runner.check_risk()
    assert runner.stop.is_set() and runner.ex.orders == {} and L.KILL.exists()
    assert any("stopped" in s for s in runner.sent)


def test_kill_file_stops_loop_and_cancels(runner, monkeypatch):
    monkeypatch.setattr(L, "LOOP_S", 0)
    calls = {"n": 0}
    orig = runner.quote_market

    def quote_then_kill(m, now):
        calls["n"] += 1
        orig(m, now)
        L.KILL.write_text("stop")
    monkeypatch.setattr(runner, "quote_market", quote_then_kill)
    runner.run()
    assert calls["n"] == 1 and runner.ex.orders == {}


def test_refuses_to_start_with_kill_file(runner):
    L.KILL.write_text("x")
    with pytest.raises(SystemExit):
        runner.run()


def test_live_flag_needs_env(monkeypatch):
    monkeypatch.delenv("MM_LIVE", raising=False)
    monkeypatch.setattr(sys, "argv", ["live_mm.py", "--live", "--markets", "c1"])
    with pytest.raises(SystemExit, match="MM_LIVE"):
        L.main()


def test_bot_never_touches_orders_outside_its_markets(runner):
    runner.ex.orders["manual"] = {"id": "manual", "cond": "other", "token": "T", "side": "BUY", "price": 0.3, "size": 10}
    runner.quote_market(runner.markets[0], 1000)
    runner.cancel_own()
    assert set(runner.ex.orders) == {"manual"}
    runner.kill("test")
    assert "manual" in runner.ex.orders


def test_inventory_never_offered_below_raw_mid():
    """Pilot 1 book: bid 0.60 / ask 0.78 with thin top levels; the reward mid was 0.61 and the
    first version would have offered 45.92 YES at 0.62."""
    bids = [(0.60, 30), (0.55, 500)]
    asks = [(0.78, 30), (0.80, 500)]
    want, _ = L.targets(dict(M, min_size=100), bids, asks, 45.92, 0, 1000, cash=12.35, capital=45)
    price, size = want[("Y", "SELL")]
    assert price >= 0.69 and size == pytest.approx(45.92)
