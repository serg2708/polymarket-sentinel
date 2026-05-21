"""Prometheus metrics for all services.

Each service that imports this module gets a /metrics HTTP endpoint
on the port defined in METRICS_PORT env (default varies by service).

Usage:
    from packages.common.metrics import start_metrics_server, ALERTS_TOTAL, PRICE_TICKS_TOTAL
    start_metrics_server(port=8001)
    ALERTS_TOTAL.labels(kind="arb_xplatform").inc()
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram, start_http_server
import structlog

log = structlog.get_logger()

# ── Counters ──────────────────────────────────────────────────────────────

ALERTS_TOTAL = Counter(
    "polysentinel_alerts_total",
    "Total alerts fired",
    ["kind"],
)

PRICE_TICKS_TOTAL = Counter(
    "polysentinel_price_ticks_total",
    "Total price tick records inserted",
    ["source"],
)

WS_RECONNECTS_TOTAL = Counter(
    "polysentinel_ws_reconnects_total",
    "WebSocket reconnection count",
    ["venue"],
)

KALSHI_REQUESTS_TOTAL = Counter(
    "polysentinel_kalshi_requests_total",
    "Kalshi API requests",
    ["endpoint", "status"],
)

LLM_JUDGE_TOTAL = Counter(
    "polysentinel_llm_judge_total",
    "LLM judge calls",
    ["result"],  # 'approved' | 'rejected' | 'failed'
)

# ── Gauges ────────────────────────────────────────────────────────────────

APPROVED_PAIRS = Gauge(
    "polysentinel_approved_pairs",
    "Number of approved cross-platform market pairs",
)

ACTIVE_MARKETS = Gauge(
    "polysentinel_active_markets",
    "Number of active markets tracked",
    ["source"],
)

WS_CONNECTED = Gauge(
    "polysentinel_ws_connected",
    "1 if Polymarket WebSocket is currently connected",
)

ALERT_QUEUE_DEPTH = Gauge(
    "polysentinel_alert_queue_depth",
    "Items in the Redis alert queue",
)

# ── Histograms ────────────────────────────────────────────────────────────

INGEST_LATENCY = Histogram(
    "polysentinel_ingest_latency_seconds",
    "Time to fetch and insert a price record",
    ["source"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

DETECTION_LOOP_DURATION = Histogram(
    "polysentinel_detection_loop_seconds",
    "Time for one full detection loop iteration",
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)

ARB_EDGE_BPS = Histogram(
    "polysentinel_arb_edge_bps",
    "Edge in basis points for detected arbs",
    ["kind"],
    buckets=[50, 100, 150, 200, 300, 500, 750, 1000, 2000],
)


def start_metrics_server(port: int = 8000) -> None:
    try:
        start_http_server(port)
        log.info("prometheus_metrics_server_started", port=port)
    except OSError as exc:
        log.warning("prometheus_port_in_use", port=port, error=str(exc))
