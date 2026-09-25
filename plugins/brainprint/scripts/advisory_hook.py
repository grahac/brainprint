#!/usr/bin/env python3
"""Inject the owner's rendered, recommendation-only advisory once per session."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def advisory_enabled() -> bool:
    return os.environ.get("BRAINPRINT_ADVISORY", "1").lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


def in_scope(config: dict, cwd: str | None) -> bool:
    """No `scope` means everywhere. Same rule as the Pi extension: a path boundary, not a prefix."""
    if "scope" not in config:
        return True
    if not cwd:
        return False
    dirs = [Path(d).expanduser().resolve() for d in config["scope"]]
    here = Path(cwd).resolve()
    return any(here.is_relative_to(d) for d in dirs)


def advisory_path(cwd: str | None) -> Path | None:
    """Same resolution as pipeline/config.py; this copy runs outside the engine tree."""
    if os.environ.get("BRAINPRINT_CONFIG"):
        path = Path(os.environ["BRAINPRINT_CONFIG"]).expanduser()
    elif os.environ.get("BRAINPRINT_HOME"):
        path = Path(os.environ["BRAINPRINT_HOME"]).expanduser() / "brainprint.config.json"
    else:
        path = Path.home() / ".brainprint" / "brainprint.config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    if not in_scope(config, cwd):
        return None
    return path.parent / Path(config["brain_dir"]).expanduser() / "plugin" / "advisory.md"


def main() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        print("{}")
        return 0

    if not advisory_enabled():
        print("{}")
        return 0

    event_name = event.get("hook_event_name")
    if event_name and event_name != "SessionStart":
        print("{}")
        return 0

    try:
        path = advisory_path(event.get("cwd"))
        if path is None:
            print("{}")
            return 0
        advisory = path.read_text(encoding="utf-8").strip()
    except OSError:
        print("{}")
        return 0

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": advisory,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
