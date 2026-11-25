"""live_prices.json: written for open positions, throttled, atomic, and never breaks monitoring."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import trader  # noqa: E402


class FakeClient:
    def __init__(self, q): self.q, self.calls = q, 0
    def ltp(self, syms): self.calls += 1; return {s: self.q.get(s) for s in syms}


def make(tmp_path, q, pos=True):
    t = trader.Trader.__new__(trader.Trader)
    t.client, t.notify, t.paper = FakeClient(q), lambda *_: None, True
    p = {"spot": "NSE:LTF26DECFUT", "future": "NSE:LTF26DECFUT", "side": "LONG", "stop": 1, "backup_id": None}
    t.state = {"books": {b: {"position": (p if pos and b == "1H BREAKOUT" else None)} for b in trader.BOOKS}}
    t.check = lambda *a: None
    trader.LIVE_PRICES = tmp_path / "live_prices.json"
    return t


def test_written_and_throttled(tmp_path):
    t = make(tmp_path, {"NSE:LTF26DECFUT": 293.4})
    t.check_all()
    d = json.loads(trader.LIVE_PRICES.read_text())
    assert d["prices"] == {"NSE:LTF26DECFUT": 293.4} and d["time"].endswith("+05:30")
    t.client.q["NSE:LTF26DECFUT"] = 300.0
    t.check_all()                                     # within 3 s: not rewritten
    assert json.loads(trader.LIVE_PRICES.read_text())["prices"]["NSE:LTF26DECFUT"] == 293.4
    t._live_prices_t = time.time() - 10
    t.check_all()
    assert json.loads(trader.LIVE_PRICES.read_text())["prices"]["NSE:LTF26DECFUT"] == 300.0
    assert not (tmp_path / "live_prices.tmp").exists()


def test_no_positions_no_file(tmp_path):
    t = make(tmp_path, {}, pos=False)
    t.check_all()
    assert not trader.LIVE_PRICES.exists()


def test_failure_does_not_break_monitoring(tmp_path):
    t = make(tmp_path, {"NSE:LTF26DECFUT": 293.4})
    trader.LIVE_PRICES = tmp_path / "missing-dir" / "live_prices.json"
    seen = []
    t.check = lambda *a: seen.append(a)
    t.check_all()                                     # write fails, check still runs
    assert seen and seen[0][1] == 293.4
