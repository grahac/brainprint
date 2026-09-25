import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from pipeline import decisions, score, sessions
from tests.testutil import EVAL_DIR, EVAL_OWNER, LEGACY_OWNER, items_in

BRAIN_COMPARE = Path(EVAL_DIR or "", "brain_compare.py")
PAGE = "- Prefers the smaller change.\n"


def case(i, options=None, user="a"):
    return {"id": f"c{i}", "ts": "2026-06-02T12:00:00+00:00", "agent_turn": f"Should I do {i}, or do you want b?",
            "user_reply": "a", "options": options or {"a": "first", "b": "second"}, "user_option": user}


class Picker:
    """Answers the pick prompt with `answers[id]` (default "a")."""

    def __init__(self, answers=None):
        self.answers, self.prompts = answers or {}, []

    def __call__(self, prompt):
        self.prompts.append(prompt)
        return json.dumps([{"id": it["id"], "pick": self.answers.get(it["id"], "a"), "confidence": 0.7}
                           for it in items_in(prompt)])


class ScoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        private = Path(self.tmp.name) / "private"
        self.cfg = {"owner_name": "Dana", "private_dir": private}
        (private / "holdout").mkdir(parents=True)
        (private / "holdout" / "page.md").write_text(PAGE)

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self, rows):
        sessions.write_pairs(self.cfg["private_dir"] / decisions.DECISIONS_FILE, rows)

    def run_score(self, runner, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = score.run(self.cfg, runner, **kw)
        return result, out.getvalue(), err.getvalue()

    def saved(self):
        return json.loads((self.cfg["private_dir"] / score.SCORE_FILE).read_text())

    def test_scorer_reads_the_page_with_the_choices_section(self):
        (self.cfg["private_dir"] / "holdout" / "choices.md").write_text("# How Dana answers choices\n")
        self.seed([case(i) for i in range(2)])
        picker = Picker()
        self.run_score(picker, min_decisions=2)
        self.assertIn(PAGE.strip() + "\n\n# How Dana answers choices", picker.prompts[0])

    def test_best_candidate_is_chosen_on_half_a_and_scored_on_half_b(self):
        holdout = self.cfg["private_dir"] / "holdout"
        for i, text in enumerate(("weak page", "GOOD page", "weak page two"), 1):
            (holdout / f"page-{i}.md").write_text(text)
        (holdout / "page.md").write_text("weak page")
        self.seed([case(i, user="b") for i in range(40)])

        class ReadsThePage(Picker):
            def __call__(self, prompt):
                self.prompts.append(prompt)
                pick = "b" if "GOOD page" in prompt else "a"
                return json.dumps([{"id": it["id"], "pick": pick, "confidence": 0.7} for it in items_in(prompt)])

        picker = ReadsThePage()
        result, out, _ = self.run_score(picker, min_decisions=30)
        half_a, half_b = score.halves(decisions.cases(sessions.read_pairs(
            self.cfg["private_dir"] / decisions.DECISIONS_FILE)))
        self.assertEqual("page-2.md", result["chosen"])
        self.assertEqual({"page-1.md": 0.0, "page-2.md": 1.0, "page-3.md": 0.0}, result["half_a"])
        self.assertEqual((len(half_b), 1.0), (result["n"], result["accuracy"]))
        self.assertEqual("GOOD page", (holdout / "page.md").read_text())
        b_ids = {r["id"] for r in half_b}
        for prompt in picker.prompts:
            if "GOOD page" not in prompt or not ({it["id"] for it in items_in(prompt)} & b_ids):
                continue
            self.assertTrue({it["id"] for it in items_in(prompt)} <= b_ids)
        self.assertFalse(any({it["id"] for it in items_in(q)} & b_ids for q in picker.prompts
                             if "weak page" in q), "half B was used to choose")

    def test_production_threshold_is_thirty(self):
        self.assertEqual(30, score.MIN_DECISIONS)

    def test_29_decisions_report_no_accuracy_and_name_the_count(self):
        self.seed([case(i) for i in range(29)] + [case(99, user=decisions.OTHER)])
        picker = Picker()
        result, out, _ = self.run_score(picker, holdout_pairs=500)
        saved = self.saved()
        self.assertEqual(29, saved["n"])
        self.assertNotIn("accuracy", saved)
        self.assertEqual(500, saved["holdout_pairs"])
        self.assertIn("not enough recent decisions to score yet (found 29)", out)
        self.assertEqual(saved, result)
        self.assertEqual([], picker.prompts)

    def test_three_of_four_right_is_75_percent_against_50_percent_chance(self):
        self.seed([case(i) for i in range(4)])
        result, out, _ = self.run_score(Picker({"c3": "b"}), min_decisions=4, holdout_pairs=120)
        self.assertEqual({"n": 4, "accuracy": 0.75, "chance": 0.5, "with_agent_rec": 0, "combined": 0.75,
                          "holdout_pairs": 120}, self.saved())
        self.assertEqual(self.saved(), result)
        self.assertIn("75%", out)

    def test_combined_takes_the_agent_rec_when_there_is_one_and_the_brain_otherwise(self):
        rows = [case(i) for i in range(4)]
        rows[0]["user_option"] = rows[1]["user_option"] = "b"
        rows[0]["agent_rec"] = "b"              # rec right, brain (a) wrong
        rows[1]["agent_rec"] = "a"              # rec wrong, brain wrong
        rows[2]["agent_rec"] = decisions.NONE   # no rec, brain right
        self.seed(rows)
        result, out, _ = self.run_score(Picker(), min_decisions=4)
        self.assertEqual(0.5, result["accuracy"])
        self.assertEqual(2, result["with_agent_rec"])
        self.assertEqual(0.5, result["agent_rec"])
        self.assertEqual(0.75, result["combined"])
        self.assertIn("75%", out)

    def test_holdout_pairs_is_omitted_when_not_supplied(self):
        self.seed([case(i) for i in range(2)])
        self.run_score(Picker(), min_decisions=2)
        self.assertNotIn("holdout_pairs", self.saved())

    def test_chance_is_mean_of_one_over_option_count(self):
        self.seed([case(0), case(1, options={"a": "x", "b": "y", "c": "z", "d": "w"})])
        self.run_score(Picker(), min_decisions=2)
        self.assertAlmostEqual((0.5 + 0.25) / 2, self.saved()["chance"])

    def test_pick_outside_options_is_skipped_and_retried_not_counted_wrong(self):
        self.seed([case(i) for i in range(4)])
        with self.assertRaises(score.ScoreError):
            self.run_score(Picker({"c2": "nope"}), min_decisions=4)
        self.assertFalse((self.cfg["private_dir"] / score.SCORE_FILE).exists())
        retry = Picker()
        self.run_score(retry, min_decisions=4)
        self.assertEqual(["c2"], [it["id"] for p in retry.prompts for it in items_in(p)])
        self.assertEqual(1.0, self.saved()["accuracy"])

    def test_a_malformed_pick_is_retried_once_in_the_same_run(self):
        self.seed([case(i) for i in range(4)])

        class Flaky(Picker):
            def __call__(self, prompt):
                out = super().__call__(prompt)
                return out.replace('"pick": "a"', '"pick": "nope"', 1) if len(self.prompts) == 1 else out

        picker = Flaky()
        result, _, _ = self.run_score(picker, min_decisions=4)
        self.assertEqual(1.0, result["accuracy"])
        self.assertEqual(2, len(picker.prompts))
        self.assertEqual(1, len(items_in(picker.prompts[1])))

    def test_picks_run_in_batches_of_ten_and_are_not_repeated(self):
        self.seed([case(i) for i in range(23)])
        first = Picker()
        self.run_score(first, min_decisions=23)
        self.assertEqual([3, 10, 10], sorted(len(items_in(p)) for p in first.prompts))
        again = Picker()
        self.run_score(again, min_decisions=23)
        self.assertEqual([], again.prompts)

    def test_a_changed_page_is_repicked(self):
        self.seed([case(i) for i in range(2)])
        self.run_score(Picker(), min_decisions=2)
        (self.cfg["private_dir"] / "holdout" / "page.md").write_text("- new page\n")
        again = Picker()
        self.run_score(again, min_decisions=2)
        self.assertEqual(1, len(again.prompts))

    def test_prompt_names_the_owner_and_never_legacy_owner(self):
        prompt = score.pick_prompt("Dana", PAGE, [case(0)])
        self.assertIn("Dana's decision brain:\n" + PAGE, prompt)
        self.assertIn("how Dana decides", prompt)
        self.assertNotIn(LEGACY_OWNER, prompt)

    @unittest.skipUnless(EVAL_DIR and EVAL_OWNER and BRAIN_COMPARE.is_file(), "set BRAINPRINT_EVAL_DIR and BRAINPRINT_EVAL_OWNER")
    def test_eval_owner_prompt_matches_brain_compare(self):
        spec = importlib.util.spec_from_file_location("brain_compare", str(BRAIN_COMPARE))
        bc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bc)
        with tempfile.TemporaryDirectory() as gen:
            (Path(gen) / "brain_page.md").write_text(PAGE)
            bc.GENERATED = Path(gen)  # never read that repo's private/
            turn = f"Should I merge?\n🧠 {EVAL_OWNER}'s Brain recommends: merge (confidence: high)\nWhy: green.\nOr hold?"
            options = {"merge": "merge now", "hold": "wait"}
            theirs = bc.pick_prompt("gen_page", [{"id": "x1", "turn": turn, "options": options}])
        ours = score.pick_prompt(EVAL_OWNER, PAGE, [{"id": "x1", "agent_turn": turn, "options": options}])
        self.assertEqual(theirs, ours)


if __name__ == "__main__":
    unittest.main()
