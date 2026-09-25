import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from pipeline import decisions, sessions
from tests.testutil import LEGACY_OWNER, items_in

YIELD = "I rebased the branch and CI is green. Should I merge it, or do you want to hold?"
LISTED = "Two ways to fix the flaky test:\n1. Retry it in CI\n2. Fix the race in the fixture\nThoughts"
LETTERED = "Pick a storage layer:\nA) SQLite\nB) Postgres\n(going with defaults otherwise)"
DONE = "Done, tests pass."


def pair(day, turn, reply="merge it"):
    return {"ts": f"2026-{day}T12:00:00+00:00", "platform": "pi", "cwd": "/w",
            "agent_turn": turn, "user_reply": reply}


class Scorer:
    """Answers the options prompt: two options, the user picks `pick(case)` (default "a")."""

    def __init__(self, pick=lambda case: "a"):
        self.pick, self.prompts = pick, []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return "Here you go:\n" + json.dumps(
            [{"id": c["id"], "options": {"a": "first", "b": "second"}, "user_option": self.pick(c),
              "agent_rec": "a"}
             for c in items_in(prompt)])

    def asked(self):
        return [c for p in self.prompts for c in items_in(p)]


class ParseArrayTest(unittest.TestCase):
    def test_reads_the_first_array_and_ignores_text_after_it(self):
        out = 'Here:\n[{"id": 1}]\nNote: case 2 had [two] readings.'
        self.assertEqual([{"id": 1}], decisions.parse_array(out))

    def test_no_array_is_an_error(self):
        with self.assertRaises(ValueError):
            decisions.parse_array("no json here")


class CandidateTest(unittest.TestCase):
    def test_closing_question_to_the_user_is_a_candidate(self):
        self.assertTrue(decisions.is_candidate(YIELD))

    def test_numbered_options_are_a_candidate(self):
        self.assertTrue(decisions.is_candidate(LISTED))

    def test_lettered_options_are_a_candidate(self):
        self.assertTrue(decisions.is_candidate(LETTERED))

    def test_a_status_report_is_not_a_candidate(self):
        self.assertFalse(decisions.is_candidate(DONE))

    def test_one_numbered_line_is_not_a_candidate(self):
        self.assertFalse(decisions.is_candidate("Fixed:\n1. the flaky test"))

    def test_an_advisory_line_does_not_make_a_turn_a_candidate(self):
        turn = "Done, tests pass.\n🧠 Dana's brain recommends: ship it (confidence: high)\nWhy: do you want it?"
        self.assertFalse(decisions.is_candidate(turn))

    def test_strip_advisory_removes_the_line_and_its_why(self):
        turn = "Should I merge?\n🧠 Dana's brain recommends: merge (confidence: high)\nWhy: green CI.\nMore."
        self.assertEqual("Should I merge?\nMore.", decisions.strip_advisory(turn))


class BuildTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"owner_name": "Dana", "private_dir": Path(self.tmp.name) / "private",
                    "cutoff": "2026-06-01", "holdout_days": 30}
        self.path = self.cfg["private_dir"] / decisions.DECISIONS_FILE

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, rows, runner):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            counts = decisions.build(self.cfg, rows, runner)
        return counts, err.getvalue()

    def written(self):
        return sessions.read_pairs(self.path)

    def test_pre_cutoff_yield_never_enters_the_decision_set(self):
        rows = [pair("05-31", YIELD, "old"), pair("06-01", YIELD, "new"), pair("06-02", DONE)]
        scorer = Scorer()
        counts, _ = self.build(rows, scorer)
        self.assertEqual(["new"], [c["reply"] for c in scorer.asked()])
        self.assertEqual(["new"], [r["user_reply"] for r in self.written()])
        self.assertEqual(1, counts["candidates"])

    def test_option_outside_the_list_is_dropped_with_a_warning_and_retried(self):
        rows = [pair("06-02", YIELD, "one"), pair("06-03", LISTED, "two")]
        counts, err = self.build(rows, Scorer(lambda c: "zzz" if c["reply"] == "two" else "a"))
        self.assertIn("skipped malformed", err)
        self.assertEqual(["one"], [r["user_reply"] for r in self.written()])
        self.assertEqual(1, counts["kept"])
        retry = Scorer()
        counts, _ = self.build(rows, retry)
        self.assertEqual(["two"], [c["reply"] for c in retry.asked()])
        self.assertEqual(2, counts["kept"])

    def test_other_is_kept_on_disk_counted_separately_and_not_a_case(self):
        rows = [pair("06-02", YIELD, "neither"), pair("06-03", LISTED, "two")]
        counts, _ = self.build(rows, Scorer(lambda c: decisions.OTHER if c["reply"] == "neither" else "b"))
        self.assertEqual({"candidates": 2, "extracted": 2, "no_choice": 0, "other": 1, "kept": 1}, counts)
        self.assertEqual(["two"], [r["user_reply"] for r in decisions.cases(self.written())])

    def test_no_choice_is_counted_and_not_a_case(self):
        def scorer(prompt):
            return json.dumps([{"id": c["id"], "options": {}, "user_option": decisions.OTHER, "agent_rec": decisions.NONE}
                               for c in items_in(prompt)])
        counts, _ = self.build([pair("06-02", LISTED)], scorer)
        self.assertEqual(1, counts["no_choice"])
        self.assertEqual([], decisions.cases(self.written()))

    def test_agent_rec_must_be_an_offered_option_or_none(self):
        opts = {"a": "first", "b": "second"}
        self.assertTrue(decisions.valid({"options": opts, "user_option": "a", "agent_rec": "b"}))
        self.assertTrue(decisions.valid({"options": opts, "user_option": "a", "agent_rec": decisions.NONE}))
        self.assertFalse(decisions.valid({"options": opts, "user_option": "a", "agent_rec": "c"}))
        self.assertFalse(decisions.valid({"options": opts, "user_option": "a"}))

    def test_rows_extracted_before_agent_rec_are_asked_again(self):
        rows = [pair("06-02", YIELD, "r2")]
        self.build(rows, Scorer())
        path = self.cfg["private_dir"] / decisions.DECISIONS_FILE
        old = [{k: v for k, v in r.items() if k != "agent_rec"} for r in self.written()]
        path.write_text("".join(json.dumps(r) + "\n" for r in old))
        again = Scorer()
        self.build(rows, again)
        self.assertEqual(1, len(again.asked()))
        self.assertEqual("a", self.written()[0]["agent_rec"])

    def test_rerun_asks_nothing_and_batches_hold_fifteen(self):
        rows = [pair(f"06-{d:02d}", YIELD, f"r{d}") for d in range(2, 22)]
        first = Scorer()
        self.build(rows, first)
        self.assertEqual([5, 15], sorted(len(items_in(p)) for p in first.prompts))
        again = Scorer()
        counts, _ = self.build(rows, again)
        self.assertEqual([], again.prompts)
        self.assertEqual(20, counts["kept"])

    def test_prompt_names_the_owner_and_never_legacy_owner(self):
        turn = YIELD + f"\n🧠 {LEGACY_OWNER}'s Brain recommends: merge (confidence: high)\nWhy: green."
        scorer = Scorer()
        self.build([pair("06-02", turn)], scorer)
        self.assertIn("Dana", scorer.prompts[0])
        self.assertNotIn(LEGACY_OWNER, scorer.prompts[0])


if __name__ == "__main__":
    unittest.main()
