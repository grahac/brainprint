"""R12: the engine tree carries no personal data: no machine paths, session ids, or owner words.

Owner words come from BRAINPRINT_PII_WORDS (comma-separated) so this file names nobody.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".githooks" / "pre-commit"

OWNER_WORDS = [w.strip() for w in os.environ.get("BRAINPRINT_PII_WORDS", "").split(",") if w.strip()]
UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
GENERIC = [
    re.compile(r"rollout-\S*" + UUID, re.IGNORECASE),
    re.compile(UUID, re.IGNORECASE),
    re.compile(r"/(?:Users|home)/[A-Za-z0-9_-][A-Za-z0-9._-]*/"),   # a home path names a machine user
]
SKIP_DIRS = {".git", "__pycache__", "private", "brain", "scratch"}


def _skip_file(name):
    return name.endswith(".db") or (name.startswith("pairs") and name.endswith(".jsonl"))


def _allowed(rel, line):
    if rel == "LICENSE":
        return True
    return rel == "README.md" and "copyright" in line.lower()


def scan(root, words=()):
    """Return 'path:line: text' for every owner word, home path or session id under root."""
    root = Path(root)
    patterns = [re.compile(re.escape(w), re.IGNORECASE) for w in words] + GENERIC
    hits = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for name in sorted(filenames):
            if _skip_file(name):
                continue
            path = Path(dirpath) / name
            rel = path.relative_to(root).as_posix()
            if any(p.search(rel) for p in patterns):
                hits.append(f"{rel}:0: (path)")
            text = path.read_text(encoding="utf-8", errors="ignore")
            for n, line in enumerate(text.splitlines(), 1):
                if not _allowed(rel, line) and any(p.search(line) for p in patterns):
                    hits.append(f"{rel}:{n}: {line.strip()[:120]}")
    return hits


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_owner_word_in_python_file_fails_with_path(self):
        (self.tmp / "pipeline").mkdir()
        (self.tmp / "pipeline" / "x.py").write_text('SRC = "~/dev/ada_projects"\n')
        hits = scan(self.tmp, ["ada_projects"])
        self.assertEqual(len(hits), 1, hits)
        self.assertIn("pipeline/x.py:1", hits[0])

    def test_home_paths_fail(self):
        (self.tmp / "a.py").write_text('A = "/' + 'Users/ada/dev/x"\nB = "/' + 'home/ada/x"\nC = "~/dev/x"\n'
                                       'D = "fixtures/home/.claude/x"\n')
        self.assertEqual(len(scan(self.tmp)), 2)

    def test_session_ids_fail(self):
        sid = "-".join(["0a1b2c3d", "1111", "2222", "3333", "444455556666"])
        (self.tmp / "a.md").write_text(f"see rollout-2026-08-03T10-00-00-{sid}.jsonl\nsession {sid}\n")
        self.assertEqual(len(scan(self.tmp)), 2)

    def test_license_with_copyright_holder_passes(self):
        (self.tmp / "LICENSE").write_text("Copyright (c) 2026 Ada Lovelace\n")
        (self.tmp / "README.md").write_text("Copyright (c) 2026 Ada Lovelace\n")
        self.assertEqual(scan(self.tmp, ["lovelace"]), [])

    def test_synthetic_owner_passes(self):
        (self.tmp / "t.py").write_text('OWNER = "Ada"\nsession = "s1"\n')
        self.assertEqual(scan(self.tmp), [])

    def test_real_engine_tree_is_clean(self):
        hits = scan(ROOT, OWNER_WORDS)
        self.assertEqual(hits, [], "owner words / home paths / session ids in engine tree:\n" + "\n".join(hits))

    def test_license_is_mit_with_holder(self):
        text = (ROOT / "LICENSE").read_text()
        self.assertIn("MIT License", text)
        self.assertRegex(text, r"Copyright \(c\) 2026 \S")


class PreCommitHookTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        hooks = self.repo / ".hooks"
        hooks.mkdir()
        shutil.copy2(HOOK, hooks / "pre-commit")
        self.git("init", "-q")
        self.git("config", "core.hooksPath", ".hooks")
        self.git("config", "user.email", "ada@example.com")
        self.git("config", "user.name", "Ada")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True)

    def commit(self, rel):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
        self.git("add", "-f", rel)
        return self.git("commit", "-q", "-m", "t")

    def test_hook_is_executable(self):
        self.assertTrue(os.access(HOOK, os.X_OK))

    def test_rejects_private_pairs(self):
        r = self.commit("private/pairs.jsonl")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("private/pairs.jsonl", r.stdout + r.stderr)

    def test_rejects_brain_at_any_depth(self):
        for rel in ["brain/page.md", "x/brain/page.md"]:
            r = self.commit(rel)
            self.assertNotEqual(r.returncode, 0, rel)
            self.assertIn(rel, r.stdout + r.stderr)
            self.git("reset", "-q")

    def test_rejects_db_pairs_and_scratch(self):
        for rel in ["data/cache.db", "pairs-2026.jsonl", "scratch/n.txt"]:
            r = self.commit(rel)
            self.assertNotEqual(r.returncode, 0, rel)
            self.git("reset", "-q")

    def test_accepts_engine_file(self):
        r = self.commit("pipeline/sessions.py")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_does_not_reject_brain_substring(self):
        r = self.commit("pipeline/brainprint.py")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
