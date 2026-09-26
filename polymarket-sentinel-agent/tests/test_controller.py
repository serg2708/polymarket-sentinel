import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "control"))
import controller as K  # noqa: E402


@pytest.fixture
def calls(tmp_path, monkeypatch):
    log = []
    monkeypatch.setattr(K, "KILL", tmp_path / "KILL")
    monkeypatch.setattr(K, "sh", lambda args, timeout=180: (log.append(args), (0, "ok"))[1])
    monkeypatch.setattr(K, "active", lambda unit: "inactive")
    monkeypatch.setattr(K.time, "sleep", lambda s: None)
    return log


def test_stale_press_is_ignored(calls):
    assert K.handle(json.dumps({"cmd": "mm_start", "ts": 1000}), now=1000 + K.MAX_AGE_S + 1) is None
    assert calls == []


def test_garbage_is_ignored(calls):
    assert K.handle("not json", now=1) is None


def test_start_clears_kill_and_starts_live_unit(calls):
    K.KILL.write_text("x")
    out = K.handle(json.dumps({"cmd": "mm_start", "ts": 1000}), now=1001)
    assert not K.KILL.exists()
    assert ["systemctl", "--user", "start", K.LIVE_UNIT] in calls and "mm_start" in out


def test_stop_stops_live_unit_only(calls):
    K.run("mm_stop")
    assert calls == [["systemctl", "--user", "stop", K.LIVE_UNIT]]


def test_kill_writes_file(calls):
    out = K.run("mm_kill")
    assert K.KILL.exists() and "KILL set" in out


def test_agent_run_is_non_blocking(calls):
    K.run("agent_run")
    assert calls == [["systemctl", "--user", "start", "--no-block", "polysentinel-agent.service"]]


def test_unknown_command(calls):
    assert "unknown" in K.run("rm -rf /")
    assert calls == []


def test_reply_is_escaped(calls, monkeypatch):
    monkeypatch.setattr(K, "run", lambda cmd: "<b>x</b>")
    assert "&lt;b&gt;" in K.handle(json.dumps({"cmd": "status", "ts": 5}), now=5)
