"""Alert message formatting for Telegram (HTML parse mode)."""
from __future__ import annotations

import html
from datetime import datetime, timezone


def e(text: str | None) -> str:
    """HTML-escape a string for Telegram HTML parse mode."""
    return html.escape(str(text or ""))


def _c(v: float) -> str:
    """Format 0-1 probability as XX.X¢"""
    return f"{v * 100:.1f}¢"


KIND_EMOJI = {
    "arb_xplatform":      "⚡",
    "arb_intramarket":    "🔄",
    "soft_edge_metaculus": "🎯",
    "soft_edge_manifold":  "📊",
    "soft_edge_llm_prior": "🤖",
    "soft_edge_predictit": "🏛️",
    "tail_risk":           "🎯",
    "wide_spread":        "📐",
    "book_imbalance":     "⚖️",
    "price_spike":        "🚀",
    "sum_deviation":      "🔢",
    "news_divergence":    "📰",
}


def format_arb_xplatform(a: dict) -> str:
    size = float(a.get("size") or 100)
    ca_total = float(a.get("ca") or 0)   # total USDC cost for `size` YES contracts
    cb_total = float(a.get("cb") or 0)   # total USDC cost for `size` NO contracts
    yes_avg = ca_total / size if size else 0
    no_avg  = cb_total / size if size else 0
    edge_bps = int(a.get("edge_bps") or 0)
    edge_pct = edge_bps / 100

    return (
        f"⚡ <b>CROSS-PLATFORM ARB</b>  <code>+{edge_bps} bps ({edge_pct:.1f}%)</code>\n"
        f"<b>{e(a.get('title'))}</b>\n\n"
        f"BUY YES @ Polymarket  avg <code>{_c(yes_avg)}</code>\n"
        f"BUY NO  @ Kalshi      avg <code>{_c(no_avg)}</code>\n\n"
        f"For <b>{size:.0f}</b> contracts:\n"
        f"  Outlay → <code>${ca_total + cb_total:.2f}</code>  "
        f"Payout → <code>${size:.0f}</code>\n"
        f"  Profit: <b>+${a.get('edge_usd', 0):.2f}</b>\n"
    )


def format_arb_intramarket(a: dict) -> str:
    size = float(a.get("size") or 100)
    cost_yes = float(a.get("cost_yes") or 0)
    cost_no  = float(a.get("cost_no") or 0)
    total    = float(a.get("total_cost") or cost_yes + cost_no)
    yes_avg  = cost_yes / size if size else 0
    no_avg   = cost_no  / size if size else 0
    edge_bps = int(a.get("edge_bps") or 0)
    edge_pct = edge_bps / 100

    return (
        f"🔄 <b>INTRA-MARKET ARB</b>  <code>+{edge_bps} bps ({edge_pct:.1f}%)</code>\n"
        f"<b>{e(a.get('title'))}</b>\n\n"
        f"YES+NO bundle cheaper than $1:\n"
        f"  YES <code>{_c(yes_avg)}</code>  +  NO <code>{_c(no_avg)}</code>\n\n"
        f"For <b>{size:.0f}</b> contracts:\n"
        f"  Outlay → <code>${total:.2f}</code>  "
        f"Payout → <code>${size:.0f}</code>\n"
        f"  Profit: <b>+${a.get('edge_usd', 0):.2f}</b>\n"
    )


def _rule_reliability(rule_notes: str | None, approved_by: str | None) -> str:
    """One-line reliability tag based on rule_notes and match origin."""
    if not rule_notes:
        if approved_by == "auto":
            return "ℹ️ Auto-matched — verify rules manually\n"
        return ""
    notes_lower = rule_notes.lower()
    if "rule diff" in notes_lower or "differ" in notes_lower:
        first_line = rule_notes.split("\n")[0][:100]
        return f"⚠️ {e(first_line)}\n"
    if "align" in notes_lower or "identical" in notes_lower or "same" in notes_lower:
        return "✅ Rules align\n"
    # Generic notes — show truncated
    return f"ℹ️ {e(rule_notes[:100])}\n"


def format_soft_edge(a: dict) -> str:
    source = a.get("source", "model")
    emoji  = KIND_EMOJI.get(a.get("kind", ""), "🎯")
    model_p    = float(a.get("model_p") or 0)
    market_ask = float(a.get("market_ask") or 0)
    edge_pp    = float(a.get("edge_pp") or 0)
    ev         = float(a.get("ev_per_dollar") or 0)
    kelly_pct  = float(a.get("kelly_fraction") or 0) * 100

    if ev > 0:
        direction = "📈 Under-priced → BUY YES"
        kelly_label = f"Kelly ¼: <code>{kelly_pct:.1f}%</code> of bankroll"
    else:
        no_ask = 1.0 - market_ask
        direction = f"📉 Over-priced → BUY NO  (<code>{_c(no_ask)}</code> per NO contract)"
        kelly_label = f"Kelly ¼: <code>{kelly_pct:.1f}%</code> of bankroll (NO position)"

    reliability = _rule_reliability(a.get("rule_notes"), a.get("approved_by"))

    source_warning = ""
    if source == "manifold":
        source_warning = "⚠️ Manifold = play money (Mana) — Kelly already halved\n"
    elif source == "predictit":
        source_warning = "⚠️ PredictIt = real money, but 10% profit fee — budget ≥3 pp edge\n"
    elif source == "llm_prior":
        source_warning = "🤖 Claude estimate — cross-check manually before acting\n"

    return (
        f"{emoji} <b>SOFT EDGE</b> — <code>{e(source.upper())}</code>\n"
        f"<b>{e(a.get('title'))}</b>\n\n"
        f"{direction}\n"
        f"{source.capitalize()}: <code>{model_p * 100:.1f}%</code>  "
        f"Market: <code>{_c(market_ask)}</code>\n"
        f"Gap: <code>{edge_pp:+.1f} pp</code>  "
        f"EV per $1: <code>{ev:+.3f}</code>\n"
        f"{kelly_label}\n"
        + source_warning
        + reliability
    )


def format_liquidity_alert(a: dict) -> str:
    kind    = a.get("kind", "")
    emoji   = KIND_EMOJI.get(kind, "📐")
    title   = a.get("title") or a.get("token_id", "unknown")
    outcome = a.get("outcome", "")
    body = ""

    if kind == "wide_spread":
        spread_c = a.get("spread", 0) * 100
        rel_pct  = a.get("rel_spread", 0) * 100
        body = (
            f"Spread: <code>{spread_c:.1f}¢</code>  "
            f"({rel_pct:.1f}% of mid)\n"
            f"Bid: <code>{_c(a.get('best_bid', 0))}</code>  "
            f"Ask: <code>{_c(a.get('best_ask', 0))}</code>\n"
        )

    elif kind == "book_imbalance":
        direction = a.get("direction", "")
        token = (outcome or "").lower()  # "yes" or "no"
        buying_yes = (direction == "bid_heavy" and token == "yes") or \
                     (direction == "ask_heavy" and token == "no")
        raw_mid = float(a.get("mid") or 0)
        yes_price = raw_mid if token == "yes" else (1.0 - raw_mid)
        ratio = a.get("ratio", 0)
        bid = a.get("bid_size", 0)
        ask = a.get("ask_size", 0)
        if buying_yes:
            body = (
                f"📈 Smart money backing YES  <code>{ratio:.1f}×</code>\n"
                f"YES @ <code>{_c(yes_price)}</code>  Volume: <code>${max(bid, ask):.0f}</code>\n"
            )
        else:
            body = (
                f"📉 Smart money backing NO  <code>{ratio:.1f}×</code>\n"
                f"YES @ <code>{_c(yes_price)}</code>  Volume: <code>${max(bid, ask):.0f}</code>\n"
            )
        outcome = ""

    elif kind == "price_spike":
        current = float(a.get("current_mid") or 0)
        mean    = float(a.get("rolling_mean") or 0)
        std     = float(a.get("rolling_std") or 0)
        n       = int(a.get("n_samples") or 0)
        body = (
            f"Z-score: <code>{a.get('z_score', 0):+.2f}σ</code>  "
            f"({n} ticks)\n"
            f"Now: <code>{_c(current)}</code>  "
            f"Mean: <code>{_c(mean)}</code>  "
            f"±<code>{std * 100:.2f}¢</code>\n"
        )

    elif kind == "sum_deviation":
        yes_mid = float(a.get("yes_mid") or 0)
        no_mid  = float(a.get("no_mid") or 0)
        total   = float(a.get("total") or 0)
        dev_bps = int(a.get("deviation_bps") or 0)
        dir_txt = "📉 Sum < 1  (buy YES+NO bundle)" if a.get("direction") == "under" else "📈 Sum > 1"
        body = (
            f"{dir_txt}\n"
            f"YES: <code>{_c(yes_mid)}</code>  "
            f"NO: <code>{_c(no_mid)}</code>  "
            f"Sum: <code>{total * 100:.1f}¢</code>  "
            f"Δ <code>{dev_bps} bps</code>\n"
        )

    outcome_tag = f" · <code>{e(outcome)}</code>" if outcome else ""
    header = f"{emoji} <b>{e(kind.upper().replace('_', ' '))}</b>{outcome_tag}\n<b>{e(title)}</b>\n"
    return header + body


def format_tail_risk(a: dict) -> str:
    market_p   = float(a.get("market_p") or 0)
    claude_p   = float(a.get("claude_p") or 0)
    confidence = float(a.get("confidence") or 0)
    edge_pp    = float(a.get("edge_pp") or 0)
    ev         = float(a.get("ev_per_dollar") or 0)
    kelly_pct  = float(a.get("kelly_fraction") or 0) * 100
    key_signal = a.get("key_signal") or ""
    has_flow   = a.get("has_order_flow", False)

    flow_line = "⚡ Order flow confirms — unusual buying pressure\n" if has_flow else ""
    # Suggested bet: $5–$50 range based on confidence
    if confidence >= 0.90:
        suggest = "$20–$50"
    elif confidence >= 0.80:
        suggest = "$10–$25"
    else:
        suggest = "$5–$10"

    return (
        f"🎯 <b>TAIL RISK — UNDERPRICED</b>\n"
        f"<b>{e(a.get('title'))}</b>\n\n"
        f"📈 <b>BUY YES @ {_c(market_p)}</b>\n"
        f"Claude est: <code>{claude_p * 100:.1f}%</code>  "
        f"Gap: <code>+{edge_pp:.1f} pp</code>  "
        f"EV: <code>+{ev:.2f}$/1$</code>\n"
        f"Confidence: <code>{confidence * 100:.0f}%</code>  "
        f"Kelly ½: <code>{kelly_pct:.1f}%</code>\n\n"
        f"💡 {e(key_signal)}\n"
        + flow_line
        + f"💰 Suggested bet: <b>{suggest}</b> YES  (→ Polymarket ↑)\n"
    )


def format_news_divergence(a: dict) -> str:
    direction  = a.get("direction", "L")  # "L" = bearish, "H" = bullish on YES
    market_p   = float(a.get("market_p") or 0)
    sentiment  = float(a.get("sentiment_ema") or 0)
    confidence = float(a.get("confidence") or 0)
    n_articles = int(a.get("n_articles_scored") or 0)
    reason     = a.get("reason") or ""
    articles   = a.get("recent_articles") or []

    if direction == "YES_underpriced":
        action = f"📈 <b>BUY YES @ {_c(market_p)}</b>"
        sentiment_label = "positive"
    else:
        no_price = 1.0 - market_p
        action = f"📉 <b>BUY NO @ {_c(no_price)}</b>"
        sentiment_label = "negative"

    headlines = []
    for art in articles[:2]:
        if isinstance(art, dict):
            title = art.get("title") or art.get("url") or ""
            if title:
                headlines.append(f"  • {e(title[:100])}")

    headlines_block = ("\n" + "\n".join(headlines)) if headlines else ""

    reason_line = f"💡 {e(reason[:300])}\n" if reason.strip() else ""
    return (
        f"📰 <b>NEWS DIVERGENCE</b>  (conf {confidence * 100:.0f}%)\n"
        f"<b>{e(a.get('title') or a.get('group_key'))}</b>\n\n"
        f"{action}\n"
        f"Sentiment: <code>{sentiment:+.3f}</code> ({sentiment_label})"
        f"  Articles: <code>{n_articles}</code>\n\n"
        + reason_line
        + headlines_block + ("\n" if headlines_block else "")
        + "💰 Suggested: <b>$5–$15</b>  (→ Polymarket ↑)\n"
    )


def format_alert(a: dict) -> str:
    kind = a.get("kind", "")
    if kind == "arb_xplatform":
        return format_arb_xplatform(a)
    elif kind == "arb_intramarket":
        return format_arb_intramarket(a)
    elif kind in ("soft_edge_metaculus", "soft_edge_manifold",
                  "soft_edge_llm_prior", "soft_edge_predictit"):
        return format_soft_edge(a)
    elif kind == "tail_risk":
        return format_tail_risk(a)
    elif kind in ("wide_spread", "book_imbalance", "price_spike", "sum_deviation"):
        return format_liquidity_alert(a)
    elif kind == "news_divergence":
        return format_news_divergence(a)
    else:
        return f"⚠️ <b>{e(kind.upper())}</b>\n<pre>{e(str(a)[:500])}</pre>"


def format_status(ingest_ok: bool, ws_reconnects: int, last_alert_ts: str | None) -> str:
    tick = "✅" if ingest_ok else "❌"
    return (
        f"<b>PolySentinel Status</b>\n"
        f"Ingest: {tick}\n"
        f"WS reconnects (24h): <code>{ws_reconnects}</code>\n"
        f"Last alert: <code>{last_alert_ts or 'never'}</code>\n"
    )
