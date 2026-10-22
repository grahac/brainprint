# brainprint

Generates a decision brain from your own coding-agent session history.

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

## Config (`brainprint.config.json`)

- `miner_cmd`: the model that reads your history. The default uses Pi's default model (or Haiku when Pi is absent); point it at a cheap model, e.g. `pi -p --no-session -nc -ne -ns -nt --model <provider>/<model>`.
- `scorer_cmd`: the model that scores the brain (Sonnet by default).
- `scope` (optional): a list of directories; the advisory is injected only in sessions under them. Absent means everywhere.

Session text is scrubbed of secrets before it reaches either model.

The advisory is the brain's one-page summary plus a short "how you answer choices" section, mined from the turns where an agent put a choice to you. The miner writes three candidate pages; half of your recent decisions picks the best one and the other half measures it, so the reported score is not inflated by the choice. `brainprint score` reports the brain alone, the agent's own recommendation alone, and taking the agent's recommendation when it made one and the brain otherwise.

Run tests: `python3 -m unittest discover -s tests -t .` and `node --test tests/test_pi_advisory.ts`.
