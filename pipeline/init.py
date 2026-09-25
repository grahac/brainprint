"""`brainprint init | score | refresh` (R5, R7, R8, spec §5.1).

init:    config -> pairs -> held-out brain + score -> installed brain -> install
score:   pairs (reused when present) -> held-out brain + score; never installs
refresh: pairs -> installed brain -> install
Every stage caches its work on disk, so a failed run resumes where it stopped.
"""
import argparse
import os
import subprocess
import sys
from pathlib import Path

from pipeline import config, decisions, llm, mine, score, sessions

INSTALL_SH = Path(__file__).resolve().parent.parent / "install.sh"
# Default session stores, relative to the user's HOME.
STORES = {"codex": ".codex/sessions", "claude_code": ".claude/projects", "pi": ".pi/agent/sessions"}


def fail(message, code=1):
    print(f"brainprint: {message}", file=sys.stderr)
    sys.exit(code)


def run_stage(name, fn, *args, errors=(mine.MineError, score.ScoreError, llm.LLMError)):
    """Run one model-backed stage; a failure names the stage and exits non-zero."""
    try:
        return fn(*args)
    except errors as e:
        fail(f"{name} failed: {e}\n  re-run to resume; finished work is cached on disk.")


def default_owner():
    user = os.environ.get("USER")
    if not user:
        fail("$USER is not set; pass --owner NAME.", 2)
    return user[:1].upper() + user[1:]


def write_config(path, owner):
    """Write a new config for the session stores that exist under HOME."""
    stores = {name: Path.home() / rel for name, rel in STORES.items()}
    found = {name: str(p) for name, p in stores.items() if p.is_dir()}
    if not found:
        no_pairs(stores, {name: 0 for name in stores})
    cfg = {"owner_name": owner or default_owner(), "brain_dir": ".", "private_dir": "private",
           "session_sources": found, **config.detect_backends(),
           "holdout_days": 21, "max_pairs": 20000}
    path.parent.mkdir(parents=True, exist_ok=True)
    sessions.write_json(path, cfg)
    print(f"wrote {path}")


def no_pairs(sources, counts):
    lines = "\n".join(f"  {name:<12} {counts.get(name, 0)} pairs  {path}"
                      + ("" if Path(path).is_dir() else "  (not found)")
                      for name, path in sources.items())
    fail(f"no session pairs found, so there is nothing to mine. Stores checked:\n{lines}")


def pairs(cfg, reuse=False):
    """Rows from `<private_dir>/pairs.jsonl` (rebuilt unless `reuse` and it exists); stops on zero."""
    path = Path(cfg["private_dir"]) / sessions.PAIRS_FILE
    if reuse and path.is_file():
        rows = sessions.read_pairs(path)
        counts = sessions.count_by_platform(rows, cfg["session_sources"])
    else:
        rows, counts = sessions.build(cfg)
    if not rows:
        no_pairs(cfg["session_sources"], counts)
    print("pairs: " + ", ".join(f"{name} {n}" for name, n in counts.items()) + f" (total {len(rows)})")
    return rows


def held_out(cfg, rows, miner, scorer):
    """Mine the pre-cutoff brain and score it on post-cutoff decisions. Returns the pre-cutoff count."""
    cutoff = config.cutoff(cfg)
    before, _ = sessions.split(rows, cutoff)
    if not before:
        print(f"no pairs before the cutoff {cutoff}, so there is no held-out brain to score yet")
        return 0
    run_stage("held-out brain (mining)", mine.holdout, cfg, rows, miner)
    run_stage("held-out decisions (extraction)", decisions.build, cfg, rows, scorer,
              errors=(llm.LLMError, ValueError))
    result = run_stage("held-out score", score.run, cfg, scorer, len(before),
                       errors=(score.ScoreError, llm.LLMError, ValueError))
    if "message" in result:
        print("re-run `brainprint score` later")
    return len(before)


def install_sh(cfg):
    """Render the plugin and link /decide and the Pi extension into the harnesses under HOME."""
    r = subprocess.run(["bash", str(INSTALL_SH)], capture_output=True, text=True)
    print(r.stdout, end="")
    print(r.stderr, end="", file=sys.stderr)
    if r.returncode != 0:
        fail(f"install failed (install.sh exited {r.returncode}); fix the above and run `brainprint refresh`.")


def build_and_install(cfg, rows, miner, scorer, installer):
    run_stage("installed brain (mining)", mine.installed, cfg, rows, miner)
    run_stage("installed page (choosing)", score.choose_installed, cfg, scorer,
              errors=(score.ScoreError, llm.LLMError, ValueError))
    installer(cfg)


def show_page(cfg):
    page = Path(cfg["brain_dir"]) / "brain" / "page.md"
    print(f"\n--- {page} ---\n{page.read_text().strip()}\n")


def consent(cfg, yes):
    """Say what leaves the machine and where, and stop unless the user agrees (or passed --yes)."""
    print(f"brainprint sends scrubbed excerpts of your {', '.join(cfg['session_sources'])} session history "
          f"to the models behind these commands:\n  miner:  {cfg['miner_cmd']}\n  scorer: {cfg['scorer_cmd']}\n"
          "Secrets are redacted first; code, file paths and project names are sent as they are.")
    if yes:
        return
    try:
        answer = input("Continue? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        fail("stopped; nothing was sent. Run `brainprint init --yes` to skip this question.", 1)


def main(argv=None, miner=None, scorer=None, installer=None):
    """Entry point; tests inject the miner and scorer runners and the installer."""
    parser = argparse.ArgumentParser(prog="brainprint", description="Generate and install a decision brain.")
    sub = parser.add_subparsers(dest="command", required=True)
    init_p = sub.add_parser("init", help="config, pairs, held-out score, mine and install")
    init_p.add_argument("--owner", help="owner name for a new config (default: $USER, capitalized)")
    init_p.add_argument("--yes", action="store_true", help="agree to send session excerpts without asking")
    sub.add_parser("score", help="re-run the held-out score; never installs")
    sub.add_parser("refresh", help="regenerate and reinstall the brain")
    args = parser.parse_args(argv)

    try:
        path = config.config_path()
        if args.command == "init" and not path.is_file():
            write_config(path, args.owner)
        elif args.command == "init":
            print(f"using existing {path}")
        cfg = config.load()
    except config.ConfigError as e:
        fail(str(e).removeprefix("brainprint: "), 2)
    if args.command == "init":
        consent(cfg, args.yes)
    miner = miner or llm.runner(cfg["miner_cmd"])
    scorer = scorer or llm.runner(cfg["scorer_cmd"])
    installer = installer or install_sh

    if args.command == "score":
        held_out(cfg, pairs(cfg, reuse=True), miner, scorer)
        return 0
    rows = pairs(cfg)
    if args.command == "init":
        n_before = held_out(cfg, rows, miner, scorer)
        build_and_install(cfg, rows, miner, scorer, installer)
        print(f"the installed brain was mined from all {len(rows)} pairs; "
              f"the score's brain used the {n_before} pre-cutoff pairs")
    else:
        build_and_install(cfg, rows, miner, scorer, installer)
    show_page(cfg)
    print("run `brainprint refresh` to regenerate the brain later")
    return 0
