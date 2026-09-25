import contextlib
import datetime
import importlib.util
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

from pipeline import mine
from tests.testutil import EVAL_DIR, EVAL_OWNER, LEGACY_OWNER

EVAL = Path(EVAL_DIR or "", "pipeline_test.py")


def rows(n, start=0, text="ok"):
    day = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    return [{"ts": (day + datetime.timedelta(hours=i)).isoformat(), "platform": "pi", "cwd": "/w",
             "agent_turn": f"option {i}? {text}", "user_reply": f"reply {i}"}
            for i in range(start, start + n)]


class Stub:
    """Records every prompt; answers by prompt kind; raises for prompts containing `fail`."""

    def __init__(self, fail=None):
        self.prompts, self.fail, self.lock = [], fail, threading.Lock()

    def __call__(self, prompt):
        with self.lock:
            self.prompts.append(prompt)
        if self.fail and self.fail in prompt:
            raise RuntimeError("model down")
        if prompt.startswith(mine.MINE_PROMPT):
            return "- note"
        if prompt.startswith(mine.MERGE_PROMPT):
            return "- merged"
        return "brain words"

    def kinds(self):
        mines = sum(p.startswith(mine.MINE_PROMPT) for p in self.prompts)
        merges = sum(p.startswith(mine.MERGE_PROMPT) for p in self.prompts)
        return mines, merges, len(self.prompts) - mines - merges


class MineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.notes, self.out = root / "private" / "notes", root / "brain" / "brain"

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, groups, runner, out=None, owner="Dana"):
        with contextlib.redirect_stderr(io.StringIO()):
            return mine.build(groups, out or self.out, owner, runner, self.notes)

    def test_250_pairs_write_three_notes_and_all_three_brains(self):
        stub = Stub()
        self.build([rows(250)], stub)
        self.assertEqual(3, len(list(self.notes.glob("*.md"))))
        for size in ("full", "page", "paragraph"):
            self.assertEqual("brain words", (self.out / f"{size}.md").read_text())
        self.assertEqual((3, 0, 5), stub.kinds())

    def test_turns_that_put_a_choice_also_write_the_choices_section(self):
        chosen = rows(150, text="Should I ship it now, or do you want me to wait for review?")
        stub = Stub()
        self.build([rows(100) + chosen], stub)
        choice = [q for q in stub.prompts if q.startswith(mine.CHOICE_PROMPT)]
        self.assertEqual(2, len(choice))  # 150 choice exchanges, 100 per slice
        self.assertNotIn("option 0? ok", "".join(choice))
        section = [q for q in stub.prompts if "answers choices" in q]
        self.assertEqual(1, len(section))
        self.assertIn('"How Dana answers choices"', section[0])
        self.assertIn("must never read as permission", section[0])
        self.assertEqual("brain words", (self.out / mine.CHOICES).read_text())
        self.assertEqual("brain words\n\nbrain words", mine.page_text(self.out))
        self.assertEqual(5, len(list(self.notes.glob("*.md"))))  # 3 decision + 2 choice notes
        again = Stub()
        self.build([rows(100) + chosen], again)
        self.assertEqual([], again.prompts)

    def test_adding_the_section_does_not_regenerate_the_page(self):
        chosen = rows(100, text="Should I ship it now, or do you want me to wait for review?")
        notes = mine.mine_notes([chosen], self.notes, Stub())
        with contextlib.redirect_stderr(io.StringIO()):
            mine.synthesize(notes, self.out, "Dana", Stub())   # a brain built before the section existed
        stub = Stub()
        self.build([chosen], stub)
        self.assertFalse(any(q.startswith("Below is material on how") for q in stub.prompts), stub.prompts)
        self.assertTrue((self.out / mine.CHOICES).is_file())

    def test_no_choice_turns_write_no_section_and_page_text_is_the_page(self):
        self.build([rows(50)], Stub())
        self.assertFalse((self.out / mine.CHOICES).exists())
        self.assertEqual("brain words", mine.page_text(self.out))

    def test_choice_turns_and_replies_are_clipped(self):
        long = rows(1, text="x" * 3000 + " Should I ship it now, or do you want me to wait for review?")
        long[0]["user_reply"] = "y" * 3000
        (clipped,) = mine.choice_rows(long)
        self.assertEqual(mine.CHOICE_TURN_CHARS, len(clipped["agent_turn"]))
        self.assertEqual(mine.CHOICE_REPLY_CHARS, len(clipped["user_reply"]))
        self.assertTrue(clipped["agent_turn"].endswith("to wait for review?"))

    def test_three_page_candidates_and_page_md_starts_as_the_first(self):
        self.build([rows(50)], Stub())
        self.assertEqual(["page-1.md", "page-2.md", "page-3.md"], mine.page_candidates(self.out))
        (self.out / "page-2.md").write_text("second")
        self.assertEqual("brain words", (self.out / "page.md").read_text())
        mine.use_page(self.out, "page-2.md")
        self.assertEqual("second", mine.page_text(self.out))
        self.assertEqual("second", mine.page_text(self.out, "page-2.md"))

    def test_rerun_with_two_notes_present_mines_only_the_missing_slice(self):
        self.build([rows(250)], Stub())
        (self.notes / mine.note_name(mine.slice_text(rows(250)[200:]))).unlink()
        stub = Stub()
        self.build([rows(250)], stub)
        self.assertEqual(1, stub.kinds()[0])
        self.assertIn("reply 249", stub.prompts[0])

    def test_unchanged_rerun_makes_no_calls_and_leaves_page_untouched(self):
        self.build([rows(250)], Stub())
        page = self.out / "page.md"
        before = page.stat().st_mtime_ns
        stub = Stub()
        self.build([rows(250)], stub)
        self.assertEqual([], stub.prompts)
        self.assertEqual(before, page.stat().st_mtime_ns)

    def test_new_pairs_mine_only_the_new_slice_and_rewrite_page(self):
        self.build([rows(200)], Stub())
        (self.out / "page.md").write_text("stale")
        stub = Stub()
        self.build([rows(300)], stub)
        self.assertEqual((1, 0, 5), stub.kinds())
        self.assertIn("reply 299", stub.prompts[0])
        self.assertEqual("brain words", (self.out / "page.md").read_text())

    def test_installed_run_after_holdout_mines_only_post_cutoff_slices(self):
        pre, post = rows(150), rows(100, start=150)
        self.build([pre], Stub(), out=self.out.parent / "holdout")
        stub = Stub()
        self.build([pre, post], stub)
        self.assertEqual((1, 0, 5), stub.kinds())
        self.assertIn("reply 150", stub.prompts[0])
        self.assertEqual(3, len(list(self.notes.glob("*.md"))))
        self.assertTrue((self.out / "page.md").exists())

    def test_failed_slice_is_reported_others_written_and_retried_next_run(self):
        data = rows(100) + rows(100, start=100, text="BOOM") + rows(50, start=200)
        stub = Stub(fail="BOOM")
        with self.assertRaises(mine.MineError) as err:
            self.build([data], stub)
        self.assertEqual(1, len(err.exception.failed))
        self.assertEqual(2, len(list(self.notes.glob("*.md"))))
        self.assertEqual((3, 0, 0), stub.kinds())
        self.assertFalse((self.out / "page.md").exists())
        retry = Stub()
        self.build([data], retry)
        self.assertEqual((1, 0, 5), retry.kinds())
        self.assertIn("BOOM", retry.prompts[0])

    def test_45_notes_merge_one_level_of_three_groups(self):
        stub = Stub()
        self.build([rows(4500)], stub)
        self.assertEqual((45, 3, 5), stub.kinds())
        self.assertEqual(3, len(list((self.out / "merged").glob("*.md"))))

    def test_cfg_holdout_and_installed_write_to_their_dirs(self):
        root = Path(self.tmp.name)
        cfg = {"owner_name": "Dana", "brain_dir": root / "b", "private_dir": root / "b" / "private",
               "cutoff": "2026-01-05"}
        data = rows(150)  # hourly from 2026-01-01: 96 before the cutoff, 54 after
        stub = Stub()
        with contextlib.redirect_stderr(io.StringIO()):
            mine.holdout(cfg, data, stub)
            mine.installed(cfg, data, stub)
        self.assertTrue((root / "b" / "private" / "holdout" / "page.md").is_file())
        self.assertTrue((root / "b" / "brain" / "page.md").is_file())
        self.assertEqual((2, 0, 10), stub.kinds())
        self.assertEqual(2, len(list((root / "b" / "private" / "notes").glob("*.md"))))

    def test_manifest_records_input_note_hashes(self):
        self.build([rows(250)], Stub())
        manifest = json.loads((self.out / mine.MANIFEST).read_text())
        self.assertEqual(sorted(p.name for p in self.notes.glob("*.md")), sorted(manifest["notes"]))

    def test_prompts_use_owner_name_never_legacy_owner(self):
        stub = Stub()
        self.build([rows(250)], stub, owner="Dana")
        writes = [p for p in stub.prompts
                  if not p.startswith((mine.MINE_PROMPT, mine.MERGE_PROMPT))]
        self.assertEqual(5, len(writes))  # full, three page candidates, paragraph
        for p in writes:
            self.assertIn("Dana", p)
        for p in stub.prompts:
            self.assertNotIn(LEGACY_OWNER, p)


@unittest.skipUnless(EVAL_DIR and EVAL_OWNER and EVAL.is_file(), "set BRAINPRINT_EVAL_DIR and BRAINPRINT_EVAL_OWNER")
class PromptEquivalenceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("eval_pipeline_test", str(EVAL))
        cls.pt = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.pt)

    def test_prompts_match_eval_for_eval_owner(self):
        self.assertEqual(self.pt.MINE_PROMPT, mine.MINE_PROMPT)
        self.assertEqual(self.pt.MERGE_PROMPT, mine.MERGE_PROMPT)
        self.assertEqual(self.pt.SIZES, mine.SIZES)
        for size in self.pt.SIZES:
            self.assertEqual(self.pt.write_prompt(size, "SOURCE"), mine.write_prompt(EVAL_OWNER, size, "SOURCE"))

    def test_slice_text_matches_eval_shape(self):
        data = rows(3)
        self.assertEqual(self.pt.slice_text([{**r, "reply": r["user_reply"]} for r in data]),
                         mine.slice_text(data))

    def test_constants_match_eval(self):
        self.assertEqual((self.pt.SLICE_SIZE, self.pt.MERGE_GROUP, self.pt.CONCURRENCY),
                         (mine.SLICE_SIZE, mine.MERGE_GROUP, mine.CONCURRENCY))


if __name__ == "__main__":
    unittest.main()
