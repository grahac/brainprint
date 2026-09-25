"""Model calls through the user's own agent CLI: prompt on stdin, from an empty temp directory."""
import os
import shlex
import subprocess
import tempfile


CONCURRENCY = 6  # model calls in flight at once, for mining, extraction and scoring


class LLMError(Exception):
    pass


def run(cmd, prompt, timeout=900):
    argv = shlex.split(cmd)
    # Advisory off so the call logs nothing; empty cwd so no project context files load.
    env = {**os.environ, "BRAINPRINT_ADVISORY": "0"}
    with tempfile.TemporaryDirectory() as cwd:
        try:
            r = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                               cwd=cwd, env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise LLMError(f"`{argv[0]}` timed out after {timeout}s") from None
    if r.returncode != 0:
        raise LLMError(f"`{argv[0]}` exited {r.returncode}; stderr: {r.stderr[-500:]}")
    out = r.stdout.strip()
    if not out:
        raise LLMError(f"`{argv[0]}` returned empty output; stderr: {r.stderr[-500:]}")
    return out


def runner(cmd):
    """A prompt -> text callable for `cmd`; callers take a runner so tests can inject a stub."""
    return lambda prompt: run(cmd, prompt)
