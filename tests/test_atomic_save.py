"""trader.atomic_write / Trader.save: a crash mid-write must leave the previous state.json intact."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import trader  # noqa: E402


class AtomicWrite(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.f = self.d / "state.json"

    def test_writes_and_leaves_no_tmp(self):
        trader.atomic_write(self.f, '{"a": 1}')
        self.assertEqual(json.loads(self.f.read_text()), {"a": 1})
        trader.atomic_write(self.f, '{"a": 2}')
        self.assertEqual(json.loads(self.f.read_text()), {"a": 2})
        self.assertEqual(sorted(p.name for p in self.d.iterdir()), ["state.json"])

    def test_crash_mid_write_keeps_old_file(self):
        trader.atomic_write(self.f, '{"position": "BANKINDIA"}')
        with mock.patch("os.fsync", side_effect=KeyboardInterrupt):     # killed after a partial write
            with self.assertRaises(KeyboardInterrupt):
                trader.atomic_write(self.f, '{"posi')
        self.assertEqual(json.loads(self.f.read_text()), {"position": "BANKINDIA"})

    def test_save_uses_atomic_write(self):
        t = trader.Trader.__new__(trader.Trader)
        t.state = {"day": "2026-11-27", "books": {}}
        with mock.patch.object(trader, "STATE_FILE", self.f):
            t.save()
        self.assertEqual(json.loads(self.f.read_text())["day"], "2026-11-27")


if __name__ == "__main__":
    unittest.main()
