import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import config, render
from tests.testutil import LEGACY_OWNER

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "plugins" / "brainprint" / "scripts" / "advisory_hook.py"
PAGE = "Verify real state first. Prefer the smallest concrete change."


def make_brain(home, owner):
    """A BRAINPRINT_HOME with a config and a generated page; returns the loaded config."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "brainprint.config.json").write_text(json.dumps({
        "owner_name": owner, "brain_dir": ".", "private_dir": "private",
        "session_sources": {}, "miner_cmd": "cat", "scorer_cmd": "cat",
        "holdout_days": 21, "max_pairs": 100,
    }))
    (home / "brain").mkdir(exist_ok=True)
    (home / "brain" / "page.md").write_text(PAGE + "\n")
    return home


class RenderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("BRAINPRINT_")}
        env["HOME"] = str(self.tmp / "home")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def installed(self, owner):
        os.environ["BRAINPRINT_HOME"] = str(make_brain(self.tmp / "home" / owner, owner))
        cfg = config.load()
        return render.install(cfg)

    def test_possessive(self):
        self.assertEqual(render.possessive("Chris"), "Chris'")
        self.assertEqual(render.possessive("Dana"), "Dana's")

    def test_advisory_names_owner(self):
        self.assertIn("Chris' brain recommends",
                      (self.installed("Chris") / "advisory.md").read_text())
        self.assertIn("Dana's brain recommends",
                      (self.installed("Dana") / "advisory.md").read_text())

    def test_advisory_carries_the_choices_section_after_the_page(self):
        home = make_brain(self.tmp / "home" / "Dana", "Dana")
        (home / "brain" / "choices.md").write_text("# How Dana answers choices\nTakes the rec.\n")
        os.environ["BRAINPRINT_HOME"] = str(home)
        text = (render.install(config.load()) / "advisory.md").read_text()
        self.assertIn(PAGE.strip() + "\n\n# How Dana answers choices", text)

    def test_install_writes_claude_and_codex_marketplaces_for_the_plugin(self):
        plugin = self.installed("Dana")
        root = plugin.parent
        claude = json.loads((root / ".claude-plugin" / "marketplace.json").read_text())
        codex = json.loads((root / ".agents" / "plugins" / "marketplace.json").read_text())
        for m in (claude, codex):
            self.assertEqual(render.MARKETPLACE, m["name"])
            self.assertEqual(["brainprint"], [p["name"] for p in m["plugins"]])
        self.assertEqual("./plugin", claude["plugins"][0]["source"])
        self.assertEqual({"source": "local", "path": "./plugin"}, codex["plugins"][0]["source"])
        self.assertEqual("Dana", claude["owner"]["name"])

    def test_advisory_has_page_and_no_placeholders(self):
        text = (self.installed("Dana") / "advisory.md").read_text()
        self.assertIn(PAGE, text)
        self.assertNotIn("{{", text)
        self.assertNotIn("}}", text)
        self.assertIn("never grants authority", text)

    def test_both_plugin_manifests_name_owner(self):
        plugin = self.installed("Chris")
        for kind in (".claude-plugin", ".codex-plugin"):
            manifest = json.loads((plugin / kind / "plugin.json").read_text())
            iface = manifest.get("interface", manifest)
            self.assertIn("Chris' brain", iface["displayName"], kind)
            self.assertIn("Chris' brain", iface["defaultPrompt"], kind)
            self.assertNotIn("{{", json.dumps(manifest), kind)

    def test_plugin_copy_carries_hook(self):
        plugin = self.installed("Dana")
        self.assertTrue((plugin / "hooks" / "hooks.json").is_file())
        self.assertTrue((plugin / "scripts" / "advisory_hook.py").is_file())

    def test_missing_page_is_an_error(self):
        home = make_brain(self.tmp / "home" / "nopage", "Dana")
        (home / "brain" / "page.md").unlink()
        os.environ["BRAINPRINT_HOME"] = str(home)
        with self.assertRaises(FileNotFoundError):
            render.install(config.load())

    def test_engine_templates_have_no_owner_name(self):
        for path in (ROOT / "plugins" / "brainprint").rglob("*"):
            if path.is_file() and path.suffix in {".md", ".json", ".py"}:
                self.assertNotIn(LEGACY_OWNER, path.read_text(), path)
        for path in (ROOT / "skills" / "decide" / "SKILL.md",
                     ROOT / "integrations" / "pi" / "brainprint-advisory.ts",
                     ROOT / "install.sh"):
            self.assertNotIn(LEGACY_OWNER, path.read_text(), path)


class HookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith("BRAINPRINT_")}
        self.env["HOME"] = str(self.tmp / "home")

    def run_hook(self, event=None, **env):
        out = subprocess.run([sys.executable, str(HOOK)],
                             input=json.dumps(event or {"hook_event_name": "SessionStart"}),
                             capture_output=True, text=True, env={**self.env, **env},
                             check=True)
        return json.loads(out.stdout)

    def rendered_home(self):
        home = make_brain(self.tmp / "home" / "bh", "Dana")
        with mock.patch.dict(os.environ, {"BRAINPRINT_HOME": str(home), "HOME": self.env["HOME"]}):
            render.install(config.load())
        return str(home)

    def test_injects_rendered_advisory(self):
        result = self.run_hook(BRAINPRINT_HOME=self.rendered_home())
        context = result["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("Dana's brain recommends", context)
        self.assertIn(PAGE, context)

    def test_disabled_is_empty(self):
        home = self.rendered_home()
        for off in ("0", "false", "OFF", "no"):
            self.assertEqual(self.run_hook(BRAINPRINT_HOME=home, BRAINPRINT_ADVISORY=off), {})

    def test_scope_limits_injection_to_listed_dirs(self):
        home = self.rendered_home()
        cfg_path = Path(home) / "brainprint.config.json"
        cfg = json.loads(cfg_path.read_text())
        cfg["scope"] = [str(self.tmp / "dev")]
        cfg_path.write_text(json.dumps(cfg))
        event = lambda cwd: {"hook_event_name": "SessionStart", "cwd": cwd}
        self.assertIn("hookSpecificOutput", self.run_hook(event(str(self.tmp / "dev" / "p")), BRAINPRINT_HOME=home))
        self.assertEqual({}, self.run_hook(event(str(self.tmp / "dev_old")), BRAINPRINT_HOME=home))
        self.assertEqual({}, self.run_hook(event(str(self.tmp / "other")), BRAINPRINT_HOME=home))

    def test_no_rendered_file_is_empty(self):
        self.assertEqual(self.run_hook(BRAINPRINT_HOME=str(self.tmp / "nothing")), {})
        home = make_brain(self.tmp / "unrendered", "Dana")
        self.assertEqual(self.run_hook(BRAINPRINT_HOME=str(home)), {})


class DecideSkillTest(unittest.TestCase):
    def test_frontmatter_and_full_brain(self):
        text = (ROOT / "skills" / "decide" / "SKILL.md").read_text()
        self.assertTrue(text.startswith("---\n"))
        front = text.split("---\n")[1]
        self.assertRegex(front, r"(?m)^name: decide$")
        self.assertRegex(front, r"(?m)^description: \S")
        self.assertIn("brain/full.md", text)
        for gone in ("predict.py", "self_improve.py", "log_prediction", "predictions.jsonl"):
            self.assertNotIn(gone, text)


if __name__ == "__main__":
    unittest.main()
