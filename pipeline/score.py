"""Score the held-out brain on the owner's post-cutoff decisions (R5, R6).

The scorer model reads the held-out page (`mine.page_text`: page.md plus choices.md) and predicts each pick in
`decisions.jsonl` (eval/brain_compare.py's pick_prompt and run_arm). Picks are saved on each
row under the page version they came from, so a rerun asks only about missing picks.
The result goes to `<private_dir>/score.json`; below MIN_DECISIONS it carries no accuracy.
`combined` takes the agent's own recommendation when it made one and the brain's pick otherwise:
the brain second-guessing a recommendation lost on the author's history, so that is the headline.
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pipeline import decisions, mine, sessions
from pipeline.llm import CONCURRENCY
from pipeline.decisions import strip_advisory
from pipeline.render import possessive

SCORE_FILE = "score.json"
PAGE = Path("holdout") / "page.md"
BATCH = 10
MIN_DECISIONS = 30


class ScoreError(Exception):
    pass


def pick_prompt(owner, page, batch):
    items = [{"id": r["id"], "agent_turn": strip_advisory(r["agent_turn"]), "options": r["options"]}
             for r in batch]
    return (
        f"{possessive(owner)} decision brain:\n{page}\n\n===== TASK =====\n"
        f"Above is what is known about how {owner} decides. Each case below is an AI coding agent's "
        f"turn that put a choice to {owner}, with the options it offered. Predict which option key "
        f"{owner} picks, and your probability that the pick is right. Answer with only a JSON array "
        '[{"id": ..., "pick": "<option key>", "confidence": <0-1>}].\n\n'
        + json.dumps(items, ensure_ascii=False))


def run_picks(rows, owner, page, runner, save, attempts=2):
    """Pick every case in `rows` lacking a pick from this page, BATCH at a time (CONCURRENCY batches
    in flight), calling save() after each batch. Picks live in row["picks"][page version], so one
    row carries picks from several candidate pages. Malformed picks are asked again, up to
    `attempts` passes in all; any still missing raise ScoreError, and the next run retries them.
    Returns the page version."""
    version = sessions.short_hash(page)
    missing = lambda: [r for r in rows if version not in r.get("picks", {})]
    for _ in range(attempts):
        todo = missing()
        batches = [{r["id"]: r for r in todo[s:s + BATCH]} for s in range(0, len(todo), BATCH)]
        with ThreadPoolExecutor(CONCURRENCY) as pool:
            replies = pool.map(lambda b: runner(pick_prompt(owner, page, b.values())), batches)
            for i, (batch, reply) in enumerate(zip(batches, replies)):
                apply_picks(batch, reply, version)
                save()
                print(f"picks {min((i + 1) * BATCH, len(todo))}/{len(todo)}", file=sys.stderr)
    if skipped := sorted(r["id"] for r in missing()):
        raise ScoreError(f"brainprint: {len(skipped)} picks were malformed; re-run to retry: {skipped}")
    return version


def apply_picks(batch, reply, version):
    """Store each valid pick on its row; malformed ones are reported and left for a retry."""
    for v in decisions.parse_array(reply):
        row = batch.get(v.get("id"))
        if not row or v.get("pick") not in row["options"]:
            print(f"skipped malformed pick for {v.get('id')}", file=sys.stderr)
            continue
        row.setdefault("picks", {})[version] = {"pick": v["pick"], "confidence": v.get("confidence")}


def halves(cases):
    """A deterministic split by decision id: half A chooses a page, half B reports its score."""
    in_a = lambda r: int(sessions.short_hash(r["id"]), 16) % 2 == 0
    return [r for r in cases if in_a(r)], [r for r in cases if not in_a(r)]


def accuracy(rows, version):
    return sum(r["picks"][version]["pick"] == r["user_option"] for r in rows) / len(rows)


def choose(rows, owner, out_dir, runner, save):
    """Score every candidate page in `out_dir` on `rows`, write the best to page.md (ties go to
    the first), and return (chosen name, {name: accuracy})."""
    scores = {}
    for name in mine.page_candidates(out_dir):
        version = run_picks(rows, owner, mine.page_text(out_dir, name), runner, save)
        scores[name] = accuracy(rows, version)
    best = max(scores, key=scores.get)
    mine.use_page(out_dir, best)
    return best, scores


def report(rows, version):
    """Accuracy, the agent's own recommendation, and the combined rule on `rows`."""
    brain = lambda r: r["picks"][version]["pick"]
    has_rec = lambda r: r.get("agent_rec", decisions.NONE) != decisions.NONE
    with_rec = [r for r in rows if has_rec(r)]
    combined = lambda r: r["agent_rec"] if has_rec(r) else brain(r)
    result = {"accuracy": accuracy(rows, version), "with_agent_rec": len(with_rec)}
    if with_rec:
        result["agent_rec"] = sum(r["agent_rec"] == r["user_option"] for r in with_rec) / len(with_rec)
    result["combined"] = sum(combined(r) == r["user_option"] for r in rows) / len(rows)
    return result


def run(cfg, runner, holdout_pairs=None, min_decisions=MIN_DECISIONS):
    """Score the held-out brain on `decisions.jsonl`; write and return `score.json`.
    With several candidate pages, half A of the decisions chooses the page and the reported
    numbers come from half B only, so choosing cannot inflate them. Result:
    {n (decisions reported on), chance (when n > 0), [accuracy, with_agent_rec, agent_rec,
    combined, chosen, half_a], [holdout_pairs]}, plus `message` when too few decisions."""
    private = Path(cfg["private_dir"])
    path = private / decisions.DECISIONS_FILE
    rows = sessions.read_pairs(path)
    kept = decisions.cases(rows)
    save = lambda: sessions.write_pairs(path, rows)
    out_dir = private / PAGE.parent
    if len(kept) < min_decisions:
        result = {"n": len(kept), "message": f"not enough recent decisions to score yet (found {len(kept)})"}
    else:
        chosen, half_a = None, None
        if len(mine.page_candidates(out_dir)) > 1:
            a, kept = halves(kept)
            chosen, half_a = choose(a, cfg["owner_name"], out_dir, runner, save)
        version = run_picks(kept, cfg["owner_name"], mine.page_text(out_dir), runner, save)
        result = {"n": len(kept), **report(kept, version)}
        if chosen:
            result.update(chosen=chosen, half_a=half_a)
    if result["n"]:
        result["chance"] = sum(1 / len(r["options"]) for r in kept) / result["n"]
    if holdout_pairs is not None:
        result["holdout_pairs"] = holdout_pairs
    sessions.write_json(private / SCORE_FILE, result)
    if "message" in result:
        print(result["message"])
    else:
        where = f" (page {result['chosen']}, chosen on the other half)" if "chosen" in result else ""
        print(f"held-out brain: {result['accuracy']:.0%} on {result['n']} recent decisions "
              f"(chance {result['chance']:.0%}){where}")
        rec = f", {result['agent_rec']:.0%} when taken alone" if "agent_rec" in result else ""
        print(f"with the agent's own recommendation ({result['with_agent_rec']} decisions{rec}) "
              f"and the brain otherwise: {result['combined']:.0%}")
    return result


def choose_installed(cfg, runner, min_decisions=MIN_DECISIONS):
    """Install the installed brain's best candidate page, chosen on half A of the held-out
    decisions. The installed brain has seen those sessions, so this only picks a page; the
    reported score comes from the held-out brain. Returns the chosen name, or None.

    The gate counts every decision, the same as the score's: half A is a hash split, so gating on
    its size would decline to choose on data the score just chose a page from."""
    out_dir = Path(cfg["brain_dir"]) / "brain"
    path = Path(cfg["private_dir"]) / decisions.DECISIONS_FILE
    rows = sessions.read_pairs(path) if path.is_file() else []
    cases = decisions.cases(rows)
    a, _ = halves(cases)
    if len(mine.page_candidates(out_dir)) < 2 or len(cases) < min_decisions:
        print("installed page: the first candidate (too few recent decisions to choose between pages)")
        return None
    chosen, scores = choose(a, cfg["owner_name"], out_dir, runner, lambda: sessions.write_pairs(path, rows))
    print("installed page: " + chosen + " (" + ", ".join(f"{n} {v:.0%}" for n, v in scores.items()) + ")")
    return chosen
