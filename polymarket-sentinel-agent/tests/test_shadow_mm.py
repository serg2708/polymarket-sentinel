import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mm"))
import shadow_mm as S  # noqa: E402


def test_score_matches_docs_worked_example():
    # docs: v = 3c; 1c from mid scores ((3-1)/3)^2, 2c scores ((3-2)/3)^2, outside scores 0
    assert S.score(3, 1) == pytest.approx(4 / 9)
    assert S.score(3, 2) == pytest.approx(1 / 9)
    assert S.score(3, 3) == 0 and S.score(3, 4) == 0


def test_q_min_single_sided_scores_at_third_only_in_mid_range():
    assert S.q_min(90, 0, 0.5) == pytest.approx(30)
    assert S.q_min(90, 0, 0.05) == 0


def test_adjusted_mid_ignores_orders_below_min_size():
    bids, asks = [(0.50, 5), (0.48, 500)], [(0.52, 5), (0.56, 500)]
    assert S.adjusted_mid(bids, asks, 100) == pytest.approx(0.52)


def test_our_quote_rounds_outward_to_tick():
    assert S.our_quote(0.535, 0.01) == (0.52, 0.55)
    assert S.our_quote(0.50, 0.01) == (0.49, 0.51)


def test_sample_share_alone_vs_crowded_book():
    alone = S.sample_share([(0.40, 1000)], [(0.60, 1000)], 0.50, 4.5, 20, 0.49, 0.51, 100)
    crowded = S.sample_share([(0.49, 100_000)], [(0.51, 100_000)], 0.50, 4.5, 20, 0.49, 0.51, 100)
    assert alone == pytest.approx(1.0)
    assert crowded < 0.01


@pytest.mark.parametrize("trade,expect", [
    ({"side": "SELL", "outcome": "Yes", "price": 0.48, "size": 50}, ("BUY", 0.49, 50)),    # hits our bid
    ({"side": "BUY", "outcome": "No", "price": 0.52, "size": 50}, ("BUY", 0.49, 50)),      # = sell YES at .48
    ({"side": "BUY", "outcome": "Yes", "price": 0.52, "size": 50}, ("SELL", 0.51, 50)),    # lifts our ask
    ({"side": "SELL", "outcome": "No", "price": 0.48, "size": 50}, ("SELL", 0.51, 50)),    # = buy YES at .52
    ({"side": "BUY", "outcome": "Yes", "price": 0.50, "size": 50}, None),                  # inside our spread
])
def test_apply_trade_directions(trade, expect):
    assert S.apply_trade(trade, 0.49, 0.51, 0, 100) == expect


def test_apply_trade_caps_inventory():
    t = {"side": "SELL", "outcome": "Yes", "price": 0.40, "size": 500}
    assert S.apply_trade(t, 0.49, 0.51, 80, 100) == ("BUY", 0.49, 20)
    assert S.apply_trade(t, 0.49, 0.51, 100, 100) is None


def test_event_start_from_description_and_game_time():
    m = {"description": 'scheduled on September 24, 2026 at 7:55 PM ET. This market...'}
    assert S.event_start(m) == pytest.approx(1790294100)          # 2026-09-24 23:55 UTC
    assert S.event_start({"game_start_time": "2026-09-26T18:00:00Z"}) == pytest.approx(1790445600)
    assert S.event_start({"description": "no time here"}) is None


def test_exclude_candle_markets():
    assert S.EXCLUDE.search("Bitcoin Up or Down - September 13, 11:00AM-11:05AM ET")
    assert not S.EXCLUDE.search("Will Netanyahu say Ceasefire during his remarks?")


def test_group_key_joins_sibling_brackets():
    a = S.group_key("Will MrBeast Gaming's next video get between 35 and 40 million views?")
    b = S.group_key("Will MrBeast Gaming's next video get between 20 and 22.5 million views?")
    d = S.group_key("Will MrBeast Gaming's next video get between 20 and 22.5 million views on day 1?")
    c = S.group_key("Will the highest temperature in Seoul be 26°C on September 27?")
    e = S.group_key("Will the highest temperature in Helsinki be 17°C on September 27?")
    assert a == b == d and a != c and c != e
