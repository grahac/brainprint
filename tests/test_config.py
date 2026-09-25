import contextlib
import datetime
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import config, sessions

BASE = {
    "owner_name": "Dev",
    "brain_dir": "brain",
    "private_dir": "private",
    "session_sources": {"codex": "~/.codex/sessions", "claude_code": "cc", "pi": "/abs/pi"},
    "miner_cmd": "cat",
    "scorer_cmd": "cat",
    "holdout_days": 21,
    "max_pairs": 20000,
}


def write(path, **overrides):
    data = {**BASE, **overrides}
    data = {k: v for k, v in data.items() if v is not None}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.home = self.tmp / "home"
        self.home.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("BRAINPRINT_")}
        env["HOME"] = str(self.home)
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_brainprint_config_wins(self):
        write(self.home / ".brainprint" / "brainprint.config.json", owner_name="home")
        write(self.home / "bh" / "brainprint.config.json", owner_name="bphome")
        explicit = write(self.home / "x.json", owner_name="explicit")
        os.environ["BRAINPRINT_HOME"] = str(self.home / "bh")
        os.environ["BRAINPRINT_CONFIG"] = str(explicit)
        self.assertEqual(config.load()["owner_name"], "explicit")

    def test_brainprint_home_and_relative_paths(self):
        write(self.home / ".brainprint" / "brainprint.config.json", owner_name="home")
        bh = self.home / "bh"
        write(bh / "brainprint.config.json", owner_name="bphome")
        os.environ["BRAINPRINT_HOME"] = str(bh)
        cfg = config.load()
        self.assertEqual(cfg["owner_name"], "bphome")
        brain = bh / "brain"
        self.assertEqual(cfg["brain_dir"], brain)
        self.assertEqual(cfg["private_dir"], brain / "private")
        self.assertEqual(cfg["session_sources"]["claude_code"], brain / "cc")
        self.assertEqual(cfg["session_sources"]["codex"], self.home / ".codex" / "sessions")
        self.assertEqual(cfg["session_sources"]["pi"], Path("/abs/pi"))

    def test_default_home_location(self):
        write(self.home / ".brainprint" / "brainprint.config.json", owner_name="home")
        self.assertEqual(config.load()["owner_name"], "home")

    def test_missing_config_exits_2_with_message(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            config.load()
        self.assertEqual(cm.exception.code, 2)
        expected = self.home / ".brainprint" / "brainprint.config.json"
        self.assertEqual(err.getvalue(), (
            "brainprint: no brain configured.\n"
            f"  expected:  {expected}\n"
            "  create:    brainprint init\n"
            "  or read:   the brainprint README\n"))

    def test_cutoff_from_config(self):
        write(self.home / ".brainprint" / "brainprint.config.json", cutoff="2026-08-28", holdout_days=5)
        cfg = config.load()
        self.assertEqual(config.cutoff(cfg, today=datetime.date(2030, 1, 1)), datetime.date(2026, 8, 28))

    def test_cutoff_from_holdout_days(self):
        write(self.home / ".brainprint" / "brainprint.config.json")
        cfg = config.load()
        self.assertEqual(config.cutoff(cfg, today=datetime.date(2026, 9, 23)), datetime.date(2026, 9, 2))
        self.assertEqual(config.cutoff(cfg), datetime.date.today() - datetime.timedelta(days=21))

    def test_missing_scorer_cmd_names_key(self):
        write(self.home / ".brainprint" / "brainprint.config.json", scorer_cmd=None)
        with self.assertRaisesRegex(config.ConfigError, "scorer_cmd"):
            config.load()


class HomeBoundaryTest(ConfigTest):
    def test_brain_dir_outside_home_is_rejected(self):
        os.environ["BRAINPRINT_CONFIG"] = str(write(self.home / "c.json", brain_dir="../../outside"))
        with self.assertRaisesRegex(config.ConfigError, "outside"):
            config.load()

    def test_write_json_is_atomic(self):
        target = self.home / "state" / "m.json"
        sessions.write_json(target, {"notes": ["a"]})
        sessions.write_json(target, {"notes": ["a", "b"]})
        self.assertEqual({"notes": ["a", "b"]}, json.loads(target.read_text()))
        self.assertEqual(["m.json"], [p.name for p in target.parent.iterdir()])


class DetectBackendsTest(unittest.TestCase):
    def fake_path(self, *names):
        d = Path(tempfile.mkdtemp())
        for n in names:
            p = d / n
            p.write_text("#!/bin/sh\nexit 1\n")
            p.chmod(0o755)
        return str(d)

    def test_pi_and_claude(self):
        b = config.detect_backends(path=self.fake_path("claude", "pi"))
        self.assertEqual(b["miner_cmd"], "pi -p --no-session -nc -ne -ns -nt")
        self.assertEqual(b["scorer_cmd"], "claude -p --safe-mode --tools \"\" --model sonnet --no-session-persistence")

    def test_only_claude(self):
        b = config.detect_backends(path=self.fake_path("claude"))
        self.assertEqual(b["miner_cmd"], "claude -p --safe-mode --tools \"\" --model haiku --no-session-persistence")
        self.assertEqual(b["scorer_cmd"], "claude -p --safe-mode --tools \"\" --model sonnet --no-session-persistence")

    def test_neither_raises_named(self):
        with self.assertRaisesRegex(config.ConfigError, "miner_cmd"):
            config.detect_backends(path=self.fake_path())


if __name__ == "__main__":
    unittest.main()
