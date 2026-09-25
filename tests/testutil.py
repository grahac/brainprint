"""Shared test constants and helpers."""
import json
import os

# Parity checks against the author's private eval harness run only when these are set.
EVAL_DIR = os.environ.get("BRAINPRINT_EVAL_DIR")
EVAL_OWNER = os.environ.get("BRAINPRINT_EVAL_OWNER")

# A name engine prompts must never hard-code: the eval harness's owner when set, else a stand-in.
LEGACY_OWNER = EVAL_OWNER or "Ada"


def items_in(prompt):
    """The JSON array a batch prompt ends with."""
    return json.loads(prompt[prompt.rindex("\n\n") + 2:])
