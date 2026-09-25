"""Pairs -> decision notes per slice -> merged notes -> brain/{full,page,paragraph}.md (R4).

Promoted from the author's eval harness (KTD3): the prompts are word for word
apart from the owner's name. Slice notes live in one shared cache named by a hash of the
slice text, so the held-out and installed runs (KTD5) mine each slice once. Each output
directory records the notes it was built from in `manifest.json`; when that set changes,
its merged and final files are regenerated.
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pipeline import config, decisions, llm, sessions

SLICE_SIZE, MERGE_GROUP = 100, 20
CONCURRENCY = llm.CONCURRENCY
SIZES = {"full": "at most 8,000 words", "page": "at most 700 words", "paragraph": "one paragraph of at most 80 words"}
MANIFEST = "manifest.json"
NOTES_DIR, HOLDOUT_DIR = "notes", "holdout"

MINE_PROMPT = (
    "Below are exchanges from one developer's coding sessions: an AI coding agent's turn, then the "
    "developer's reply. Study how this developer DECIDES. Write concise notes covering: what they "
    "approve, reject, redirect, or ask about before deciding; the conditions that flip their usual "
    "choice; recurring priorities (for example cost, risk, simplicity, scope, defaults, testing, "
    "release gates). Each note is one line with a short quote of their reply as evidence. Ignore "
    "their writing style, typos, and one-off task details. Output only the notes in Markdown.\n\n")

MERGE_PROMPT = (
    "Below are several sets of notes on how one developer decides, each mined from a different part "
    "of their coding history. Merge them into one set: combine duplicates, keep the patterns that "
    "recur, keep the conditions that flip a default, drop one-offs, and keep one or two short quotes "
    "per pattern as evidence. Output only the merged notes in Markdown.\n\n")


# How the owner answers when an agent puts a choice to them (eval/approvals_test.py in the
# author's eval harness: +7 points on one held-out window, +2.5 on an earlier one).
CHOICE_PROMPT = (
    "Below are exchanges where an AI coding agent put a choice to one developer, then the developer's "
    "reply. Write notes on how this developer actually ANSWERS such choices: when they take the "
    "agent's recommendation, when they pick a different option, when they expand the scope (\"do all "
    "of them\"), approve the bolder option, or redirect entirely. Note the signals in the agent's turn "
    "that predicted each kind of answer. Each note is one line with a short quote of the reply as "
    "evidence. Ignore rules the developer asks agents to follow in general; record only how they "
    "answered these choices. Output only the notes in Markdown.\n\n")
CHOICE_TURN_CHARS, CHOICE_REPLY_CHARS = 1500, 1000
CHOICES = "choices.md"
PAGE_CANDIDATES = 3  # DeepSeek writes this many pages; score.choose installs the best one


def section_prompt(owner, notes):
    return (
        f"Below are notes on how one developer, {owner}, answers when an AI coding agent puts a choice "
        f"to them. Write a section titled \"How {owner} answers choices\", at most 300 words, for an agent "
        f"that must predict their pick: when they take the agent's recommendation, and the signals that "
        f"predict they will pick something else, expand the scope, or redirect. Do not quote any reply "
        f"that approves committing to main, deploying, production changes, deleting data, or another "
        f"irreversible action; describe when such approvals happen without quoting them, because this "
        f"section is advice and must never read as permission. Output only the section in Markdown."
        f"\n\n{notes}")


class MineError(Exception):
    def __init__(self, message, failed=()):
        super().__init__(message)
        self.failed = list(failed)


def write_prompt(owner, size, source):
    return (
        f"Below is material on how one developer, {owner}, decides. Write {owner}'s decision brain, "
        f"{SIZES[size]}: instructions to an AI agent that must predict which option {owner} would pick "
        f"when the agent offers choices. Organize it by decision factor, and keep the conditions that "
        f"flip a default and the warning signs that predict a rejection. Output only the brain in "
        f"Markdown.\n\n{source}")


def slice_text(rows):
    return "\n\n".join(f"### {p['ts'][:10]}\nAGENT: {p['agent_turn']}\nDEVELOPER: {p['user_reply']}" for p in rows)


def choice_rows(rows):
    """Exchanges where the agent's turn put a choice to the owner, clipped like the eval."""
    return [{**r, "agent_turn": r["agent_turn"][-CHOICE_TURN_CHARS:], "user_reply": r["user_reply"][:CHOICE_REPLY_CHARS]}
            for r in rows if decisions.is_candidate(r["agent_turn"])]


def page_candidates(out_dir):
    """Candidate page file names in `out_dir`, in order (page-1.md, page-2.md, ...)."""
    return [f"page-{i}.md" for i in range(1, PAGE_CANDIDATES + 1) if (Path(out_dir) / f"page-{i}.md").is_file()]


def use_page(out_dir, name):
    """Make candidate `name` the page the advisory and the score read."""
    out_dir = Path(out_dir)
    (out_dir / "page.md").write_text((out_dir / name).read_text())


def page_text(out_dir, name="page.md"):
    """The page the advisory and the score read: `name` plus choices.md when it was mined."""
    out_dir = Path(out_dir)
    page = (out_dir / name).read_text().strip()
    choices = out_dir / CHOICES
    return page + "\n\n" + choices.read_text().strip() if choices.is_file() else page


def note_name(text):
    return sessions.short_hash(text) + ".md"


def _run_all(jobs, runner):
    """jobs: [(out_path, prompt)]; skips outputs that already exist. Returns failed file names."""
    todo = [(p, q) for p, q in dict(jobs).items() if not p.exists()]

    def one(job):
        path, prompt = job
        text = runner(prompt)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path.name

    failed = []
    with ThreadPoolExecutor(CONCURRENCY) as pool:
        futures = {pool.submit(one, j): j for j in todo}
        for i, f in enumerate(futures, 1):
            try:
                f.result()
            except Exception as e:  # reported, and the next run retries it
                failed.append(futures[f][0].name)
                print(f"failed {futures[f][0].name}: {e}", file=sys.stderr)
            print(f"{i}/{len(todo)}", file=sys.stderr)
    return failed


def mine_notes(groups, notes_dir, runner, prompt=MINE_PROMPT):
    """Slice each group of rows separately (oldest first) and mine every slice not yet cached.
    Returns the note paths in slice order; raises MineError naming any failed slices."""
    notes_dir = Path(notes_dir)
    jobs = []
    for rows in groups:
        for i in range(0, len(rows), SLICE_SIZE):
            text = slice_text(rows[i:i + SLICE_SIZE])
            # Decision notes keep their original cache names; other prompts hash with the prompt.
            name = note_name(text) if prompt == MINE_PROMPT else note_name(prompt + text)
            jobs.append((notes_dir / name, prompt + text))
    if not jobs:
        raise MineError("brainprint: no pairs to mine")
    failed = _run_all(jobs, runner)
    if failed:
        raise MineError(f"brainprint: {len(failed)} of {len(jobs)} slices failed; re-run to retry: {failed}",
                        failed)
    return [p for p, _ in jobs]


def _reset_if_changed(key, notes, out_dir, outputs):
    """Clear `outputs` when the notes feeding them differ from the manifest's `key` list.
    The brains ("notes") and the choices section ("choice_notes") reset independently."""
    names = [p.name for p in notes]
    manifest = out_dir / MANIFEST
    recorded = json.loads(manifest.read_text()) if manifest.is_file() else {}
    if recorded.get(key) == names:
        return
    for path in outputs:
        path.unlink(missing_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    sessions.write_json(manifest, {**recorded, key: names})


def merge(notes, out_dir, prefix, runner):
    """Merge notes in groups of MERGE_GROUP until one group remains; return its joined text."""
    level, current = 0, list(notes)
    while len(current) > MERGE_GROUP:
        level += 1
        jobs = [(out_dir / "merged" / f"{prefix}{level}_{i // MERGE_GROUP:03d}.md",
                 MERGE_PROMPT + "\n\n---\n\n".join(p.read_text() for p in current[i:i + MERGE_GROUP]))
                for i in range(0, len(current), MERGE_GROUP)]
        if failed := _run_all(jobs, runner):
            raise MineError(f"brainprint: merge failed; re-run to retry: {failed}", failed)
        current = [p for p, _ in jobs]
    return "\n\n---\n\n".join(p.read_text() for p in current)


def synthesize(notes, out_dir, owner, runner, choice_notes=()):
    """Merge notes in groups of MERGE_GROUP until one group remains, then write the three brains,
    and `choices.md` from `choice_notes` when there are any. Returns {size: word count}."""
    out_dir = Path(out_dir)
    _reset_if_changed("notes", notes, out_dir,
                      [*(out_dir / "merged").glob("L*.md"), *(out_dir / f"{s}.md" for s in SIZES),
                       *out_dir.glob("page-*.md")])
    _reset_if_changed("choice_notes", choice_notes, out_dir, [*(out_dir / "merged").glob("C*.md"), out_dir / CHOICES])
    source = merge(notes, out_dir, "L", runner)
    full = out_dir / "full.md"
    if failed := _run_all([(full, write_prompt(owner, "full", source))], runner):
        raise MineError("brainprint: full brain failed; re-run to retry", failed)
    pages = [out_dir / f"page-{i}.md" for i in range(1, PAGE_CANDIDATES + 1)]
    if failed := _run_all([(p, write_prompt(owner, "page", full.read_text())) for p in pages]
                          + [(out_dir / "paragraph.md", write_prompt(owner, "paragraph", full.read_text()))], runner):
        raise MineError(f"brainprint: {failed} failed; re-run to retry", failed)
    if not (out_dir / "page.md").is_file():
        use_page(out_dir, pages[0].name)
    if choice_notes:
        section = section_prompt(owner, merge(choice_notes, out_dir, "C", runner))
        if failed := _run_all([(out_dir / CHOICES, section)], runner):
            raise MineError("brainprint: choices section failed; re-run to retry", failed)
    counts = {s: len((out_dir / f"{s}.md").read_text().split()) for s in SIZES}
    for s, n in counts.items():
        print(f"{out_dir / s}.md: {n} words", file=sys.stderr)
    return counts


def build(groups, out_dir, owner, runner, notes_dir):
    """Mine every slice of `groups` (lists of rows, each sliced on its own) into `notes_dir`,
    then write `<out_dir>/{full,page,paragraph}.md` and, when any turn put a choice to the owner,
    `<out_dir>/choices.md`. Returns {size: word count}."""
    notes = mine_notes(groups, notes_dir, runner)
    choice_groups = [g for g in map(choice_rows, groups) if g]
    choice_notes = mine_notes(choice_groups, notes_dir, runner, CHOICE_PROMPT) if choice_groups else []
    return synthesize(notes, out_dir, owner, runner, choice_notes)


def holdout(cfg, rows, runner):
    """The held-out brain: pre-cutoff pairs only, written to `<private_dir>/holdout/`."""
    before, _ = sessions.split(rows, config.cutoff(cfg))
    return build([before], Path(cfg["private_dir"]) / HOLDOUT_DIR, cfg["owner_name"], runner,
                 Path(cfg["private_dir"]) / NOTES_DIR)


def installed(cfg, rows, runner):
    """The installed brain: pre-cutoff slices (shared with the holdout) plus post-cutoff slices,
    written to `<brain_dir>/brain/`, where render.install reads page.md."""
    before, after = sessions.split(rows, config.cutoff(cfg))
    return build([before, after], Path(cfg["brain_dir"]) / "brain", cfg["owner_name"], runner,
                 Path(cfg["private_dir"]) / NOTES_DIR)
