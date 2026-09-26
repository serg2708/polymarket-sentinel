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
    want, why = L.targets(M, *BOOK, 0, 0, 1000, capital=49, spread=0.01)
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
    want, _ = L.targets(M, *BOOK, 45.92, 0, 1000, cash=12.35, capital=45, spread=0.01)
    assert want[("Y", "SELL")] == (pytest.approx(0.51), 45.92)                           # offer the inventory
    assert ("N", "BUY") not in want
    assert ("Y", "BUY") not in want                                                      # at the inventory cap


def test_targets_sells_held_no_on_the_bid_side():
    want, _ = L.targets(M, *BOOK, 0, 30, 1000, cash=100, capital=45, spread=0.01)
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



# --- fixes after pilot 2 (Saudi / Gemini, 2026-09-26) ------------------------------------------

def test_buy_never_exceeds_inventory_cap():
    """Pilot 2 Gemini: after 30 YES filled the bot bid for another full 50 -> held 80 (cap 50)."""
    full = L.targets(M, *BOOK, 0, 0, 1000, cash=500, capital=49, spread=0.01)[0][("Y", "BUY")][1]
    want, _ = L.targets(M, *BOOK, 0, 0, 1000, cash=500, capital=49, spread=0.01)
    part, _ = L.targets(M, *BOOK, 30, 0, 1000, cash=500, capital=49, spread=0.01)
    assert ("Y", "BUY") not in part or part[("Y", "BUY")][1] <= full - 30 + 1e-9
    no_side, _ = L.targets(M, *BOOK, 0, 30, 1000, cash=500, capital=49, spread=0.01)
    assert ("N", "BUY") not in no_side or no_side[("N", "BUY")][1] <= full - 30 + 1e-9


def test_bid_never_above_raw_mid_when_big_orders_drag_the_reward_mid():
    """Pilot 2 Gemini: book 0.35/0.39 but large orders put the size-cutoff mid near 0.50;
    the bot bid 0.49 for YES and was filled at once."""
    bids = [(0.35, 30), (0.34, 60)]
    asks = [(0.39, 30), (0.65, 5000)]
    want, _ = L.targets(dict(M, min_size=50), bids, asks, 0, 0, 1000, cash=500, capital=49, spread=0.01)
    assert want[("Y", "BUY")][0] <= 0.36 + 1e-9                  # raw mid 0.37 - 1c
    assert 1 - want[("N", "BUY")][0] >= 0.38 - 1e-9              # our implied YES ask stays above raw mid


def test_paused_market_only_offers_inventory():
    want, why = L.targets(M, *BOOK, 20, 0, 1000, cash=500, capital=49, allow_buys=False)
    assert set(want) == {("Y", "SELL")}
    none, why = L.targets(M, *BOOK, 0, 0, 1000, cash=500, capital=49, allow_buys=False)
    assert none == {} and "paused" in why


def test_jumped():
    hist = [(1000, 0.50), (1030, 0.50)]
    assert not L.jumped(hist, 1040, 0.52)
    assert L.jumped(hist, 1040, 0.54)
    assert not L.jumped(hist, 1030 + L.JUMP_WINDOW_S + 5, 0.60)   # both samples outside the window


def test_no_duplicate_when_listing_lags(runner):
    """Pilot 2 Saudi: a fresh order missing from the listing made the bot place a second one."""
    m = runner.markets[0]
    runner.quote_market(m, 1000)
    assert len(runner.ex.orders) == 2
    runner.ex.hidden = set(runner.ex.orders)                     # exchange doesn't list them yet
    runner.quote_market(m, 1015)
    assert len(runner.ex.orders) == 2                            # nothing placed twice
    runner.ex.hidden = set()
    runner.quote_market(m, 1030)
    assert len(runner.ex.orders) == 2


def test_fill_pauses_buys_on_that_market(runner):
    m = runner.markets[0]
    runner.quote_market(m, 1000)
    runner.ex.bal[m["yes"]] = 20.0                               # our YES bid got hit
    runner.quote_market(m, 1015)
    sides = {(o["token"], o["side"]) for o in runner.ex.orders.values()}
    assert (m["yes"], "BUY") not in sides and (m["no"], "BUY") not in sides
    assert (m["yes"], "SELL") in sides                           # inventory is still offered
    runner.quote_market(m, 1015 + L.FILL_COOLDOWN_S + 1)
    sides = {(o["token"], o["side"]) for o in runner.ex.orders.values()}
    assert (m["yes"], "BUY") in sides                            # buys resume after the cooldown


def test_price_jump_pauses_buys(runner, monkeypatch):
    m = runner.markets[0]
    runner.quote_market(m, 1000)
    monkeypatch.setattr(L.SH, "book", lambda token: ([(0.55, 300)], [(0.57, 300)]))
    runner.quote_market(m, 1015)
    assert not any(o["side"] == "BUY" for o in runner.ex.orders.values())
