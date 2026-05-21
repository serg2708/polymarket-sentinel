from __future__ import annotations

from datetime import datetime
from typing import Any
from pydantic import BaseModel, Field
import uuid


class MarketRecord(BaseModel):
    market_id: str
    source: str
    condition_id: str | None = None
    question: str | None = None
    description: str | None = None
    slug: str | None = None
    tags: list[str] = Field(default_factory=list)
    end_date: datetime | None = None
    tick_size: float | None = None
    min_order_size: float | None = None
    fee_schedule: dict | None = None
    active: bool = True
    raw: dict | None = None


class TokenRecord(BaseModel):
    token_id: str
    market_id: str
    outcome: str  # 'YES' | 'NO'


class PriceRecord(BaseModel):
    ts: datetime
    token_id: str
    source: str
    best_bid: float | None = None
    best_ask: float | None = None
    mid: float | None = None
    last_trade: float | None = None
    bid_size_top: float | None = None
    ask_size_top: float | None = None
    liquidity: float | None = None
    volume_24h: float | None = None


class BookLevel(BaseModel):
    price: float
    size: float


class OrderBook(BaseModel):
    token_id: str
    source: str
    ts: datetime
    bids: list[BookLevel] = Field(default_factory=list)  # sorted descending by price
    asks: list[BookLevel] = Field(default_factory=list)  # sorted ascending by price

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return (self.best_bid + self.best_ask) / 2
        return None

    @property
    def spread(self) -> float | None:
        if self.best_bid is not None and self.best_ask is not None:
            return self.best_ask - self.best_bid
        return None

    def cost_to_buy(self, target_size: float) -> float | None:
        """Walk the ask side to compute average fill cost for target_size contracts."""
        remaining, cost = target_size, 0.0
        for level in self.asks:
            take = min(level.size, remaining)
            cost += take * level.price
            remaining -= take
            if remaining <= 0:
                break
        if remaining > 0:
            return None  # not enough liquidity
        return cost


class MarketMatch(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    group_key: str
    source: str
    source_id: str
    side: str = "YES"
    match_score: float | None = None
    llm_confidence: float | None = None
    rule_notes: str | None = None
    approved_by: str = "pending"


class AlertPayload(BaseModel):
    kind: str
    group_key: str
    title: str
    edge_bps: int
    edge_usd: float | None = None
    size: float | None = None
    cost: float | None = None
    ca: float | None = None       # cost leg A
    cb: float | None = None       # cost leg B
    poly_ask: float | None = None
    kalshi_ask: float | None = None
    poly_size: float | None = None
    kalshi_size: float | None = None
    poly_url: str | None = None
    kalshi_url: str | None = None
    manifold_url: str | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class WSEvent(BaseModel):
    event_type: str      # 'book' | 'price_change' | 'best_bid_ask' | 'last_trade_price' | ...
    asset_id: str
    raw: dict
