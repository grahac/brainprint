---
name: decide
description: "Predict how the brain's owner would decide a pending choice, reasoning from their generated brain (brain/full.md). Use when the user runs /decide or $decide, or asks how they would decide."
argument-hint: "[optional: the decision you're weighing — if empty, uses the current task context]"
allowed-tools: [Bash, Read]
---

# /decide — the owner's decision brain

Predict how the owner of this brain would decide, then report it back so they can check it against what they'd actually do. The prediction is an LLM review of their generated brain; there is no retrieval step and nothing is logged.

## Instructions

**1. Determine the decision context**
- If arguments were passed (`$ARGUMENTS`): use them as the situation.
- If empty: summarize the pending decision from the current task/conversation — what action is being proposed and the surrounding context. Keep it to 1-3 sentences of the *situation an agent would describe before asking the owner*.

**2. Find the brain**
The config is the first of: `$BRAINPRINT_CONFIG`; `$BRAINPRINT_HOME/brainprint.config.json`; `~/.brainprint/brainprint.config.json`. Read `owner_name` and `brain_dir` from it (a relative `brain_dir` resolves against the config file's directory). This prints the brain path and the owner:
```bash
python3 -c 'import json,os;from pathlib import Path;e=os.environ;p=Path(e["BRAINPRINT_CONFIG"]).expanduser() if e.get("BRAINPRINT_CONFIG") else Path(e.get("BRAINPRINT_HOME","~/.brainprint")).expanduser()/"brainprint.config.json";c=json.loads(p.read_text());print(p.parent/Path(c["brain_dir"]).expanduser()/"brain/full.md",c["owner_name"])'
```
If there is no config or no `brain/full.md`, stop and say: "no brain yet — run `brainprint init`."

**3. Read the brain (the whole prediction)**
Read `<brain_dir>/brain/full.md` in full and reason from it:
- Does the proposal hit one of the owner's anti-patterns or rejections recorded there?
- Which of their defaults and principles apply to this situation?
- Is there a context override in the brain (for example pre-launch, a paid client, a test baseline)?
- What would the owner's posture be — approve / reject / question / command / verify-first?

**4. Report back** — use the owner's name, be concise, direct, not sycophantic:

```
## How I'd decide: [MODE] (confidence [LOW/MEDIUM/HIGH])

**Best guess:** [1-2 sentences: what the owner would likely decide, reasoned from the brain]

**Why:** [2-3 sentences: which parts of brain/full.md drove this — cite the specific principle or pattern by name]

**Honest caveat:** [if low confidence or the brain is silent or split on this: say so plainly]
```

If the predicted mode is reject, also quote the matching rejection pattern from `brain/full.md` if one clearly applies.

## Constraints
- Don't paraphrase to sound more confident than the brain supports.
- If the brain doesn't clearly apply, say "the brain doesn't have a clear read here — ask <owner>."
- Never present a low-confidence prediction as a firm answer.
- This is advice only: it never grants authority to act.
