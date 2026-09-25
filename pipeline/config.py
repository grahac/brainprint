"""brainprint.config.json: the one file that owns every path and both model commands (spec §4.2)."""
import datetime
import json
import os
import shutil
import sys
from pathlib import Path

FILENAME = "brainprint.config.json"
REQUIRED = ("owner_name", "brain_dir", "private_dir", "session_sources",
            "miner_cmd", "scorer_cmd", "holdout_days", "max_pairs")

# Tools off: the model only reads the prompt. MINER_PI uses Pi's default model; point
# miner_cmd at a cheap one (e.g. `--model <provider>/<model>`) in the config.
SCORER_CLAUDE = 'claude -p --safe-mode --tools "" --model sonnet --no-session-persistence'
MINER_PI = "pi -p --no-session -nc -ne -ns -nt"
MINER_CLAUDE = 'claude -p --safe-mode --tools "" --model haiku --no-session-persistence'


class ConfigError(Exception):
    pass


def missing(key, where):
    return ConfigError(f"brainprint: '{key}' is not set in {where}; "
                       f"run `brainprint init` or add '{key}' by hand.")


def config_path():
    if os.environ.get("BRAINPRINT_CONFIG"):
        return Path(os.environ["BRAINPRINT_CONFIG"]).expanduser()
    if os.environ.get("BRAINPRINT_HOME"):
        return Path(os.environ["BRAINPRINT_HOME"]).expanduser() / FILENAME
    return Path.home() / ".brainprint" / FILENAME


def no_brain(path):
    print("brainprint: no brain configured.\n"
          f"  expected:  {path}\n"
          "  create:    brainprint init\n"
          "  or read:   the brainprint README", file=sys.stderr)
    sys.exit(2)


def load():
    path = config_path()
    if not path.is_file():
        no_brain(path)
    cfg = json.loads(path.read_text())
    for key in REQUIRED:
        if key not in cfg:
            raise missing(key, path)
    brain = (path.parent / Path(cfg["brain_dir"]).expanduser()).resolve()
    cfg["brain_dir"] = brain
    cfg["private_dir"] = (brain / Path(cfg["private_dir"]).expanduser()).resolve()
    home = Path.home().resolve()
    for key in ("brain_dir", "private_dir"):
        if not cfg[key].is_relative_to(home):
            raise ConfigError(f"brainprint: '{key}' resolves to {cfg[key]}, outside {home}; "
                              f"brainprint only writes under your home directory.")
    cfg["session_sources"] = {name: brain / Path(p).expanduser()
                              for name, p in cfg["session_sources"].items()}
    return cfg


def cutoff(cfg, today=None):
    """Held-out cutoff: the fixed `cutoff` date if set, else today minus `holdout_days`."""
    if "cutoff" in cfg:
        return datetime.date.fromisoformat(cfg["cutoff"])
    return (today or datetime.date.today()) - datetime.timedelta(days=cfg["holdout_days"])


def detect_backends(path=None):
    """KTD4 command strings for the agent CLIs on PATH, for `init` to write into the config."""
    has = lambda name: shutil.which(name, path=path) is not None
    if not has("claude"):
        key = "scorer_cmd" if has("pi") else "miner_cmd and scorer_cmd"
        raise missing(key, "PATH (no claude CLI found)")
    return {"miner_cmd": MINER_PI if has("pi") else MINER_CLAUDE, "scorer_cmd": SCORER_CLAUDE}
