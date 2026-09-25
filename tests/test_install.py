import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_personalization import make_brain

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"
HARNESS_ROOTS = (".pi/agent", ".codex", ".claude")
TARGETS = (
    ".pi/agent/skills/decide/SKILL.md",
    ".pi/agent/extensions/brainprint-advisory.ts",
    ".codex/skills/decide/SKILL.md",
    ".claude/commands/decide.md",
)


def tree(path):
    return sorted(str(p.relative_to(path)) for p in path.rglob("*"))


def engine_tree():
    return [tree(ROOT / d) for d in ("plugins", "skills", "integrations")]


class InstallTest(unittest.TestCase):
    """Every run uses a throwaway HOME; the real one is never touched."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.outside = self.tmp / "outside"
        self.outside.mkdir()
        (self.outside / "keep.txt").write_text("x")
        for root in HARNESS_ROOTS:
            (self.home / root).mkdir(parents=True)
        self.brain = self.home / ".brainprint"
        make_brain(self.brain, "Dana")
        self.engine_before = engine_tree()

    def install(self, **env):
        base = {k: v for k, v in os.environ.items() if not k.startswith("BRAINPRINT_")}
        base.update(HOME=str(self.home), BRAINPRINT_HOME=str(self.brain), TMPDIR=str(self.home))
        return subprocess.run(["bash", str(INSTALL)], capture_output=True, text=True,
                              env={**base, **env})

    def test_installs_all_three_harnesses_under_home_only(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        for target in TARGETS:
            path = self.home / target
            self.assertTrue(path.is_file(), target)
        self.assertIn("brain/full.md", (self.home / TARGETS[0]).read_text())
        plugin = self.brain / "plugin"
        self.assertIn("Dana's brain recommends", (plugin / "advisory.md").read_text())
        self.assertEqual(tree(self.outside), ["keep.txt"])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["home", "outside"])
        self.assertEqual(engine_tree(), self.engine_before)

    def test_prints_claude_md_snippet_and_plugin_registration(self):
        result = self.install()
        self.assertIn("CLAUDE.md", result.stdout)
        self.assertIn(f'claude plugin marketplace add "{self.brain.resolve()}"', result.stdout)
        self.assertIn("claude plugin install brainprint@brainprint-local", result.stdout)
        self.assertIn(f'codex plugin marketplace add "{self.brain.resolve()}"', result.stdout)
        self.assertIn("codex plugin add brainprint@brainprint-local", result.stdout)
        self.assertFalse((self.home / ".claude" / "CLAUDE.md").exists())

    def test_rerun_is_idempotent(self):
        self.assertEqual(self.install().returncode, 0)
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_refuses_foreign_file_and_installs_nothing(self):
        foreign = self.outside / "mine.md"
        foreign.write_text("someone else's command")
        link = self.home / ".claude" / "commands" / "decide.md"
        link.parent.mkdir(parents=True)
        link.symlink_to(foreign)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(str(link), result.stderr)
        self.assertEqual(os.readlink(link), str(foreign))
        self.assertEqual(foreign.read_text(), "someone else's command")
        self.assertFalse((self.home / TARGETS[1]).exists())
        self.assertFalse((self.brain / "plugin").exists())

    def test_refuses_foreign_regular_file(self):
        path = self.home / ".codex" / "skills" / "decide" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("other decide")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_text(), "other decide")

    def test_missing_harness_is_skipped(self):
        (self.home / ".codex").rmdir()
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.home / ".codex").exists())
        self.assertTrue((self.home / TARGETS[0]).is_file())

    def test_without_a_brain_installs_skills_and_says_to_init(self):
        result = self.install(BRAINPRINT_HOME=str(self.home / "empty"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / TARGETS[3]).is_file())
        self.assertIn("brainprint init", result.stdout)
        self.assertFalse((self.home / "empty").exists())


if __name__ == "__main__":
    unittest.main()
