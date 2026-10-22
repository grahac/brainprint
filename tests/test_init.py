"""`brainprint init | score | refresh` end to end in a throwaway HOME; the real one is never touched."""
import contextlib
import datetime
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path
from unittest import mock

from pipeline import config, decisions, init, mine, score, sessions
from tests.testutil import items_in

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_HOME = ROOT / "tests" / "fixtures" / "home"
BRAINPRINT = ROOT / "brainprint"
STORES = (".codex/sessions", ".claude/projects", ".pi/agent/sessions")
BRAIN_TEXT = "# Brain\n- Prefers the smaller change."


def answer(prompt):
    """A well-formed model answer for each pipeline prompt kind."""
    if prompt.startswith(mine.MINE_PROMPT):
        return "- approves small changes: \"do it\""
    if prompt.startswith(mine.MERGE_PROMPT):
        return "- merged notes"
    if "===== TASK =====" in prompt:
        return json.dumps([{"id": it["id"], "pick": "a", "confidence": 0.7} for it in items_in(prompt)])
    if prompt.startswith("Each case is an AI coding agent's turn"):
        return json.dumps([{"id": it["id"], "options": {"a": "first", "b": "second"}, "user_option": "a", "agent_rec": "a"}
                           for it in items_in(prompt)])
    return BRAIN_TEXT


class Stub:
    """Records every prompt and answers it with `answer`."""

    def __init__(self):
        self.prompts, self.lock = [], threading.Lock()

    def __call__(self, prompt):
        with self.lock:
            self.prompts.append(prompt)
        return answer(prompt)

    def mines(self):
        return sum(p.startswith(mine.MINE_PROMPT) for p in self.prompts)


def recent_decisions(home, n):
    """A Claude Code session with n post-cutoff turns that each put a choice to the user."""
    start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=3)
    lines = []
    for i in range(n):
        ts = start + datetime.timedelta(minutes=2 * i)
        for role, text, t in (("assistant", f"Should I ship variant {i}, or do you want the other one?", ts),
                              ("user", f"ship variant {i}", ts + datetime.timedelta(minutes=1))):
            lines.append(json.dumps({"type": role, "isSidechain": False, "cwd": "/w/recent",
                                     "timestamp": t.isoformat().replace("+00:00", "Z"),
                                     "message": {"role": role, "content": text}}))
    path = home / ".claude" / "projects" / "-w-recent" / "s.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


def fake_clis(bin_dir):
    """`claude` and `pi` executables that answer on stdout via `answer`, never a real model."""
    bin_dir.mkdir(parents=True)
    for name in ("claude", "pi"):
        path = bin_dir / name
        path.write_text(f"#!{sys.executable}\nimport sys\nsys.path.insert(0, {str(ROOT)!r})\n"
                        "from tests.test_init import answer\nsys.stdout.write(answer(sys.stdin.read()))\n")
        path.chmod(0o755)


class InitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = self.tmp / "home"
        self.bp = self.home / ".brainprint"
        self.bin = self.tmp / "bin"
        fake_clis(self.bin)
        env = {k: v for k, v in os.environ.items() if not k.startswith("BRAINPRINT_")}
        env.update(HOME=str(self.home), BRAINPRINT_HOME=str(self.bp), USER="dana", TMPDIR=str(self.tmp),
                   PATH=os.pathsep.join([str(self.bin), str(Path(sys.executable).parent), "/usr/bin", "/bin"]))
        self.env = env
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def with_stores(self):
        shutil.copytree(FIXTURE_HOME, self.home)

    def run_cmd(self, *argv, runner=None, installer=None):
        runner = runner or Stub()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = init.main(list(argv), miner=runner, scorer=runner, installer=installer)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def brain_files(self):
        return {p.name: p.read_text() for p in (self.bp / "brain").glob("*.md")}

    def test_init_writes_config_brain_score_and_rendered_advisory(self):
        self.with_stores()
        code, out, err = self.run_cmd("init", "--yes")
        self.assertEqual(0, code, err)
        cfg = json.loads((self.bp / config.FILENAME).read_text())
        self.assertEqual("Dana", cfg["owner_name"])
        self.assertEqual({"codex": str(self.home / STORES[0]), "claude_code": str(self.home / STORES[1]),
                          "pi": str(self.home / STORES[2])}, cfg["session_sources"])
        self.assertEqual((21, 20000, "private"), (cfg["holdout_days"], cfg["max_pairs"], cfg["private_dir"]))
        self.assertEqual({"full.md", "page.md", "page-1.md", "page-2.md", "page-3.md", "paragraph.md"},
                         set(self.brain_files()))
        saved = json.loads((self.bp / "private" / score.SCORE_FILE).read_text())
        self.assertEqual(0, saved["n"])
        self.assertNotIn("accuracy", saved)
        self.assertIn("not enough recent decisions to score yet (found 0)", out)
        self.assertIn("installed page: the first candidate (too few recent decisions to choose between pages)", out)
        self.assertIn("codex 1", out)
        self.assertIn("claude_code 2", out)
        self.assertIn("pi 1", out)
        self.assertIn(BRAIN_TEXT, out)
        self.assertIn("mined from all 4 pairs", out)
        self.assertIn("4 pre-cutoff pairs", out)
        self.assertIn("Dana's brain recommends", (self.bp / "plugin" / "advisory.md").read_text())
        self.assertTrue((self.home / ".claude" / "commands" / "decide.md").is_file())

    def test_thirty_recent_decisions_choose_a_page_on_half_a_and_report_half_b(self):
        self.with_stores()
        recent_decisions(self.home, 32)
        code, out, err = self.run_cmd("init", "--yes", installer=lambda cfg: None)
        self.assertEqual(0, code, err)
        saved = json.loads((self.bp / "private" / score.SCORE_FILE).read_text())
        rows = decisions.cases(sessions.read_pairs(self.bp / "private" / decisions.DECISIONS_FILE))
        half_a, half_b = score.halves(rows)
        self.assertEqual(32, len(half_a) + len(half_b))
        self.assertEqual(len(half_b), saved["n"])
        self.assertEqual(["page-1.md", "page-2.md", "page-3.md"], sorted(saved["half_a"]))
        self.assertEqual("page-1.md", saved["chosen"])  # all tie, so the first wins
        self.assertEqual((1.0, 0.5, 4), (saved["accuracy"], saved["chance"], saved["holdout_pairs"]))
        self.assertIn(f"100% on {len(half_b)} recent decisions (chance 50%) (page page-1.md, chosen on the other half)", out)
        self.assertIn("installed page: page-1.md", out)
        self.assertIn("mined from all 36 pairs", out)

    def test_second_init_reuses_finished_mining_slices(self):
        self.with_stores()
        self.assertEqual(0, self.run_cmd("init", "--yes", installer=lambda cfg: None)[0])
        again = Stub()
        code, _, err = self.run_cmd("init", "--yes", runner=again, installer=lambda cfg: None)
        self.assertEqual(0, code, err)
        self.assertEqual(0, again.mines())

    def test_score_rewrites_score_json_without_touching_brain_or_installing(self):
        self.with_stores()
        self.assertEqual(0, self.run_cmd("init", "--yes", installer=lambda cfg: None)[0])
        brain = self.brain_files()
        (self.bp / "private" / score.SCORE_FILE).unlink()
        installs = []
        code, _, err = self.run_cmd("score", installer=installs.append)
        self.assertEqual(0, code, err)
        self.assertTrue((self.bp / "private" / score.SCORE_FILE).is_file())
        self.assertEqual(brain, self.brain_files())
        self.assertEqual([], installs)

    def test_score_in_fresh_home_with_only_a_config_builds_pairs_first(self):
        self.with_stores()
        self.bp.mkdir()
        (self.bp / config.FILENAME).write_text(json.dumps({
            "owner_name": "Dana", "brain_dir": ".", "private_dir": "private",
            "session_sources": {"codex": str(self.home / STORES[0])},
            "miner_cmd": "unused", "scorer_cmd": "unused", "holdout_days": 21, "max_pairs": 100}))
        installs = []
        code, _, err = self.run_cmd("score", installer=installs.append)
        self.assertEqual(0, code, err)
        self.assertEqual(1, len(sessions.read_pairs(self.bp / "private" / sessions.PAIRS_FILE)))
        self.assertTrue((self.bp / "private" / score.SCORE_FILE).is_file())
        self.assertFalse((self.bp / "brain").exists())
        self.assertEqual([], installs)

    def test_refresh_regenerates_and_reinstalls_without_scoring(self):
        self.with_stores()
        self.assertEqual(0, self.run_cmd("init", "--yes", installer=lambda cfg: None)[0])
        (self.bp / "brain" / "page.md").unlink()
        (self.bp / "private" / score.SCORE_FILE).unlink()
        installs = []
        code, _, err = self.run_cmd("refresh", installer=installs.append)
        self.assertEqual(0, code, err)
        self.assertEqual({"full.md", "page.md", "page-1.md", "page-2.md", "page-3.md", "paragraph.md"},
                         set(self.brain_files()))
        self.assertEqual(1, len(installs))
        self.assertFalse((self.bp / "private" / score.SCORE_FILE).exists())

    def test_no_session_stores_stops_listing_paths_and_writes_no_brain(self):
        self.home.mkdir()
        code, _, err = self.run_cmd("init", "--yes")
        self.assertNotEqual(0, code)
        for store in STORES:
            self.assertIn(str(self.home / store), err)
        self.assertFalse((self.bp / "brain").exists())

    def test_mining_failure_names_the_stage_and_says_re_run(self):
        self.with_stores()

        def down(prompt):
            raise RuntimeError("model down")

        code, _, err = self.run_cmd("init", "--yes", runner=down, installer=lambda cfg: None)
        self.assertNotEqual(0, code)
        self.assertIn("held-out brain", err)
        self.assertIn("re-run to resume", err)

    def test_init_discloses_the_model_commands_and_stops_without_a_yes(self):
        self.with_stores()
        stub = Stub()
        with unittest.mock.patch("builtins.input", return_value="n"):
            code, out, err = self.run_cmd("init", runner=stub)
        self.assertEqual(1, code)
        self.assertEqual([], stub.prompts)
        self.assertFalse((self.bp / "private" / "pairs.jsonl").exists())
        cfg = json.loads((self.bp / config.FILENAME).read_text())
        self.assertIn(cfg["miner_cmd"], out)
        self.assertIn(cfg["scorer_cmd"], out)
        self.assertIn("nothing was sent", err)

    def test_init_proceeds_on_yes_at_the_prompt(self):
        self.with_stores()
        with unittest.mock.patch("builtins.input", return_value="y"):
            code, _, err = self.run_cmd("init", installer=lambda cfg: None)
        self.assertEqual(0, code, err)

    def test_dispatcher_init_completes_with_fake_clis(self):
        self.with_stores()
        r = subprocess.run([str(BRAINPRINT), "init", "--yes"], capture_output=True, text=True, env=self.env)
        self.assertEqual(0, r.returncode, r.stderr + r.stdout)
        cfg = json.loads((self.bp / config.FILENAME).read_text())
        self.assertEqual(config.MINER_PI, cfg["miner_cmd"])
        self.assertIn(BRAIN_TEXT, (self.bp / "plugin" / "advisory.md").read_text())

    def test_unknown_or_missing_subcommand_prints_usage_and_fails(self):
        for argv in (["bogus"], []):
            r = subprocess.run([str(BRAINPRINT), *argv], capture_output=True, text=True, env=self.env)
            self.assertNotEqual(0, r.returncode)
            self.assertIn("usage", r.stderr.lower())
        self.assertFalse(self.bp.exists())


if __name__ == "__main__":
    unittest.main()
