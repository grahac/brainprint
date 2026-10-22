# brainprint

Generates a decision brain from your own coding-agent session history.

Your coding agents ask you to decide things all day: merge now or wait, patch or rewrite, ship or test first. brainprint reads those exchanges from your Claude Code, Codex and Pi history, writes down how you tend to answer, and hands that back to your agents so they can recommend what you would pick.

It also measures itself. It builds a brain from your older history and tests it on the decisions you made in the last three weeks, so you know how often it would have called your choice before you lean on it.

## What you get

- **The advisory.** A one-page summary of how you decide, loaded at the start of each Claude Code, Codex and Pi session. When the agent reaches a real choice, it adds one line saying what you would likely pick. It stays quiet for routine work.
- **`/decide`** (`$decide` in Codex; also installed as a Pi skill). Asks for a full prediction on a pending choice. It reads the long version of your brain and reports what you would likely decide, which of your habits drove that, and how sure it is.

Both are advice only. Neither gives an agent permission to deploy, delete, send anything or touch production.

## Example

An agent has written a database migration for Dana and asks whether to run it now or wait for review. With the advisory installed, its reply ends with:

```text
🧠 Dana's brain recommends: wait for review, then run it on staging first (confidence: high)
Why: Dana approves each production change separately and wants a rollback path before anything mutates.
```

Dana wants the longer read, so types `/decide run the migration on production now?`:

```text
## How I'd decide: verify-first (confidence HIGH)

**Best guess:** Dana would hold the production run until the migration has passed on staging and has a written rollback.

**Why:** brain/full.md says production changes get their own approval and need a rollback path first; a schema change on a live database is exactly that case.

**Honest caveat:** If the database had no users yet, the brain's "not live yet, lower bar" exception would apply and Dana might just run it.
```

## Install

Needs `python3` and the `claude` CLI on your PATH; when `pi` is also there, it does the mining.

```bash
git clone https://github.com/grahac/brainprint.git
cd brainprint
./brainprint init
```

`init` runs `install.sh`, which writes only under `$HOME` and never overwrites a file it did not install. At the end it prints two commands that register the advisory plugin with Claude Code and Codex; run them once.

## Use

- `brainprint init` writes `~/.brainprint/brainprint.config.json`, mines your Claude Code, Pi and Codex history into a brain, scores it on your recent decisions, and installs the advisory and `/decide`. It first shows which model commands your session excerpts go to and asks before sending anything (`--yes` skips the question).
- `brainprint score` re-scores the held-out brain without installing anything.
- `brainprint refresh` regenerates the brain from new history.

## How it works

1. **Read.** It reads your session files under `~/.claude`, `~/.codex` and `~/.pi` and pairs each agent turn with your reply. Secrets are scrubbed before any text reaches a model.
2. **Mine.** The miner model writes notes on each slice of 100 pairs, merges them, and writes the brain at three lengths: `brain/full.md` (up to 8,000 words, read by `/decide`), a one-page summary (up to 700 words, the advisory) and a one-paragraph version. A short "how you answer choices" section, mined from the turns where an agent put options to you, is added to the advisory.
3. **Score.** A second brain is mined only from history older than 21 days (`holdout_days`). The scorer finds the recent turns where an agent asked you to choose, pulls out the options and the one you picked, and asks the held-out brain to pick. With at least 30 such decisions you get its accuracy next to chance. The miner writes three candidate pages; half of the recent decisions choose the best one and the other half measure it, so choosing cannot inflate the score. `brainprint score` also reports how the agent's own recommendation did, and how well taking the agent's recommendation when it made one, and the brain's otherwise, would have done.
4. **Install.** The brain you actually use is mined from all of your history, and its page is chosen the same way.

## Config (`brainprint.config.json`)

- `miner_cmd`: the model that reads your history. The default uses Pi's default model (or Haiku when Pi is absent); point it at a cheap model, e.g. `pi -p --no-session -nc -ne -ns -nt --model <provider>/<model>`.
- `scorer_cmd`: the model that scores the brain (Sonnet by default).
- `scope` (optional): a list of directories; the advisory is injected only in sessions under them. Absent means everywhere.

Turn the advisory off for one session with `export BRAINPRINT_ADVISORY=0`.

Run tests: `python3 -m unittest discover -s tests -t .` and `node --test tests/test_pi_advisory.ts`.
