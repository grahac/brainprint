"""Held-out decisions: post-cutoff turns that put a choice to the owner, with the options and pick (R5, KTD6).

A post-cutoff pair is a candidate when its agent turn ends by asking the user to decide
(`looks_like_decision_yield`, promoted from the author's original advisory tooling) or lists
two or more numbered or lettered options. The scorer model then extracts each candidate's
options, the owner's pick and the option the agent recommended (eval/jev_compare.py's options_prompt).
Results go to `<private_dir>/decisions.jsonl`; a rerun asks only about candidates not yet there.
"""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pipeline import config, sessions
from pipeline.llm import CONCURRENCY

DECISIONS_FILE = "decisions.jsonl"
BATCH = 15
OTHER = "other"
NONE = "none"  # agent_rec when the turn recommended no option

# --- Decision-yield trigger, copied from the author's original advisory tooling ---

DECISION_PATTERNS = [
    r'\?\s*$',                       # ends with a question mark
    r'\bshould i\b', r'\bshall i\b', r'\bdo you want\b', r'\bwant me to\b',
    r'\bwould you like\b', r'\blet me know\b', r'\bwhat do you think\b',
    r'\bok to proceed\?', r'\bcan i (go ahead|proceed|deploy|merge|push)\b',
    r'\bwhich (option|approach)\b', r'\bor should i\b', r'\bwould you prefer\b',
    r'\byour call\b', r'\bgo ahead\?', r'\bproceed\?', r'\bok to (deploy|merge|push)\?',
]
DECISION_RE = re.compile('|'.join(DECISION_PATTERNS), re.IGNORECASE)
SECOND_PERSON_RE = re.compile(r"\b(you|your|you'd|you're|yours)\b", re.IGNORECASE)


def final_sentence(text):
    """The last sentence of a message — where a yield actually lives."""
    parts = [p for p in re.split(r'(?<=[.!?])\s+|\n+', (text or '').strip()) if p.strip()]
    return parts[-1].strip() if parts else ''


def looks_like_decision_yield(text):
    """A yield is a question in the closing sentence that asks the user."""
    final = final_sentence(text)
    if not final:
        return False
    return bool(DECISION_RE.search(final)) and bool(SECOND_PERSON_RE.search(final))


# A line opening with "1.", "2)", "(a)", "B:", "**A)**" or "Option 2:" followed by text.
OPTION_LINE_RE = re.compile(r"^\s*(?:[-*]\s+)?(?:\*\*)?(?:option\s+)?\(?([0-9]{1,2}|[a-z])[.):]\s+\S",
                            re.IGNORECASE | re.MULTILINE)


def lists_options(text):
    return len({m.lower() for m in OPTION_LINE_RE.findall(text)}) >= 2


def strip_advisory(turn):
    """The turn without a brain-advisory line and the 'Why:' line after it (eval/jev_compare.py).
    Matches any owner's line, so an advisory the user already runs cannot leak into scoring."""
    kept, skip_why = [], False
    for line in turn.splitlines():
        if "brain recommends" in line.lower():
            skip_why = True
            continue
        if skip_why and line.strip().startswith("Why:"):
            skip_why = False
            continue
        skip_why = skip_why and not line.strip()
        kept.append(line)
    return "\n".join(kept).strip()


def is_candidate(turn):
    turn = strip_advisory(turn)
    return looks_like_decision_yield(turn) or lists_options(turn)


def pair_id(row):
    key = f"{row['ts']}\n{row['agent_turn']}\n{row['user_reply']}"
    return sessions.short_hash(key)


def candidates(cfg, rows):
    """Post-cutoff pairs whose agent turn puts a choice to the user, each with an `id`."""
    _, after = sessions.split(rows, config.cutoff(cfg))
    return [{"id": pair_id(r), **r} for r in after if is_candidate(r["agent_turn"])]


def options_prompt(owner, batch):
    items = [{"id": r["id"], "agent_turn": strip_advisory(r["agent_turn"]), "reply": r["user_reply"]}
             for r in batch]
    return (
        f"Each case is an AI coding agent's turn that may have put a choice to {owner}, and {owner}'s "
        "reply. For each case list the distinct options the turn offered (2-6, short snake_case keys "
        f"mapped to a one-line description) and which key {owner} chose. Use \"other\" for user_option "
        f"when {owner} chose none of them. A reply like \"do your rec\" means the option the turn "
        "recommended. Also give agent_rec: the key the turn itself recommended, or \"none\" if it "
        "recommended none. If the turn offered no choice, use {} for options, \"other\" for "
        "user_option and \"none\" for agent_rec. Answer with only a JSON array "
        '[{"id": ..., "options": {"key": "description"}, "user_option": ..., "agent_rec": ...}].\n\n'
        + json.dumps(items, ensure_ascii=False))


def parse_array(out):
    """The JSON array in a model answer; a reply without one fails the run (progress is saved)."""
    if "[" not in out:
        raise ValueError(f"brainprint: scorer returned no JSON array; re-run to retry: {out[:200]!r}")
    # raw_decode stops at the end of the first array, so a note after it cannot break parsing.
    return json.JSONDecoder().raw_decode(out[out.index("["):])[0]


def valid(v):
    options, pick, rec = v.get("options"), v.get("user_option"), v.get("agent_rec")
    if not isinstance(options, dict):
        return False
    if not options:
        return pick == OTHER and rec == NONE
    return len(options) >= 2 and (pick in options or pick == OTHER) and (rec in options or rec == NONE)


def cases(rows):
    """Extracted decisions to score: a real choice where the owner picked one of the options."""
    return [r for r in rows if r.get("options") and r["user_option"] != OTHER]


def counts(n_candidates, rows):
    no_choice = sum(not r["options"] for r in rows)
    kept = len(cases(rows))
    return {"candidates": n_candidates, "extracted": len(rows), "no_choice": no_choice,
            "other": len(rows) - no_choice - kept, "kept": kept}


def build(cfg, rows, runner):
    """Extract options and picks for every post-cutoff candidate not yet in `decisions.jsonl`,
    in batches of BATCH (CONCURRENCY in flight), saving after each batch. Returns counts (see `counts`)."""
    path = Path(cfg["private_dir"]) / DECISIONS_FILE
    found = candidates(cfg, rows)
    # Rows extracted before agent_rec existed are extracted again.
    done = {r["id"]: r for r in sessions.read_pairs(path) if "agent_rec" in r} if path.is_file() else {}
    # Only current candidates are kept, so a moved cutoff drops rows that no longer qualify.
    out = {c["id"]: done[c["id"]] for c in found if c["id"] in done}
    todo = [c for c in found if c["id"] not in done]
    batches = [{c["id"]: c for c in todo[s:s + BATCH]} for s in range(0, len(todo), BATCH)]
    with ThreadPoolExecutor(CONCURRENCY) as pool:
        replies = pool.map(lambda b: runner(options_prompt(cfg["owner_name"], b.values())), batches)
        for i, (batch, reply) in enumerate(zip(batches, replies)):
            for v in parse_array(reply):
                row = batch.get(v.get("id"))
                if not row or not valid(v):
                    print(f"skipped malformed options for {v.get('id')}", file=sys.stderr)
                    continue
                out[row["id"]] = {**row, "options": v["options"], "user_option": v["user_option"],
                                  "agent_rec": v["agent_rec"]}
            sessions.write_pairs(path, out.values())
            print(f"decisions {min((i + 1) * BATCH, len(todo))}/{len(todo)}", file=sys.stderr)
    sessions.write_pairs(path, out.values())
    return counts(len(found), list(out.values()))
