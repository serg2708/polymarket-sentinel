import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config as C  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Every test gets its own DB and KILL path, and no real Telegram messages."""
    monkeypatch.setattr(C, "DB_PATH", tmp_path / "agent.db")
    monkeypatch.setattr(C, "KILL_SWITCH", tmp_path / "KILL")
    monkeypatch.setattr(C, "MODE", "paper")
    sent = []
    import notify
    import resolve
    import run_agent
    for mod in (notify, resolve, run_agent):
        monkeypatch.setattr(mod, "send", lambda text, url=None: sent.append((text, url)), raising=False)
    return sent


class Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code, self.ok = data, status, status == 200
        self.text = json.dumps(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code != 200:
            raise RuntimeError(self.status_code)


def gamma_market(mid, question="Will X happen?", yes=0.4, days=10, liq=20000, closed=False,
                 outcome=None, desc="Rules. " * 10, created_days_ago=3, event="ev-" + "x"):
    end = datetime.now(timezone.utc) + timedelta(days=days)
    created = datetime.now(timezone.utc) - timedelta(days=created_days_ago)
    prices = [yes, 1 - yes] if outcome is None else ([1, 0] if outcome == 1 else [0, 1])
    return {"id": str(mid), "question": question, "description": desc,
            "outcomes": '["Yes", "No"]', "outcomePrices": json.dumps([str(p) for p in prices]),
            "clobTokenIds": json.dumps([f"y{mid}", f"n{mid}"]), "endDate": end.isoformat().replace("+00:00", "Z"),
            "startDate": created.isoformat().replace("+00:00", "Z"), "liquidityNum": liq,
            "closed": closed, "events": [{"slug": event}], "slug": f"m{mid}"}


def book(asks, min_size=5, fee_rate=0.0):
    return {"asks": sorted(asks), "min_size": min_size, "fee_rate": fee_rate}
