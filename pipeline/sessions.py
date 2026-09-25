"""Session history -> scrubbed agent-turn / human-reply pairs (R3, spec §4.4).

Readers cover Claude Code, Pi and Codex session stores. Every text is scrubbed of
secrets in full before it is truncated, so nothing unscrubbed is written or returned.
"""
import datetime
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

TURN_CHARS, REPLY_CHARS = 1500, 1000
REDACTED = "[REDACTED]"
PAIRS_FILE = "pairs.jsonl"

SECRET_RE = re.compile("|".join((
    r"sk-[A-Za-z0-9_-]{20,}",                                        # OpenAI / Anthropic style
    r"AKIA[0-9A-Z]{16}",                                            # AWS access key id
    r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}",     # GitHub
    r"xox[abposr]-[A-Za-z0-9-]{10,}",                               # Slack
    r"AIza[0-9A-Za-z_-]{30,}",                                      # Google
    r"(?:sk|rk)_live_[A-Za-z0-9]{16,}",                             # Stripe
    r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*",  # JWT
    r"(?i:bearer)\s+[A-Za-z0-9._~+/=-]{8,}",                        # Bearer tokens
    r"[A-Za-z][A-Za-z0-9+.-]*://[^\s:/@]+:[^\s@/]+@",               # scheme://user:pass@
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
    r"(?i:basic)\s+[A-Za-z0-9+/=]{8,}",                              # Basic auth
    r"npm_[A-Za-z0-9]{36}",                                         # npm
    r"glpat-[A-Za-z0-9_-]{20,}",                                    # GitLab
    # key=value / key: value, any case, quoted or not: api_key, secret, token, password, private_key.
    r"(?i:\b[\w.-]*(?:api[_-]?key|secret|token|passw(?:or)?d|private[_-]?key)[\w.-]*[\"']?\s*[:=]\s*"
    r"(?:\"[^\"\n]+\"|'[^'\n]+'|[^\s,;]+))",
    r"(?i:[\w-]*(?:key|secret|token|passw(?:or)?d)[\"']?\s*[:=]\s*(?:\"[^\"\n]+\"|'[^'\n]+'))",
)))

NON_HUMAN_MARKERS = ("<task-notification>", "<system-reminder>", "<local-command",
                     "<tool-use-id>", "<command-name>",
                     "This session is being continued from a previous conversation")


def scrub(text):
    return SECRET_RE.sub(REDACTED, text)


def looks_like_human_reply(text):
    """True only for a turn a person wrote: not a tool result, reminder, or injected context.
    Terse replies ("1", "A") count."""
    t = (text or "").strip()
    if not t:
        return False
    if t[0] in "<[":
        return False
    return not any(marker in t for marker in NON_HUMAN_MARKERS)


def _jsonl(path):
    try:
        f = open(path, encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return  # a live session file the agent moved after it was listed
    with f:
        for line in f:
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn line in a live session file


def _text(content):
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(b.get("text", "") for b in content
                        if isinstance(b, dict) and b.get("type") == "text").strip()
    return ""


def thread_messages(path, platform):
    """(role, text, ts, cwd) for the main thread of a Claude Code or Pi session."""
    cwd = None
    for obj in _jsonl(path):
        if not isinstance(obj, dict):
            continue
        cwd = obj.get("cwd") or cwd
        # isMeta (skill bodies) and isCompactSummary are written as user turns but are not the human.
        if obj.get("isSidechain") or obj.get("isMeta") or obj.get("isCompactSummary") \
                or (platform == "pi" and obj.get("type") != "message"):
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
            continue
        text = _text(msg.get("content"))
        if text:
            yield msg["role"], text, obj.get("timestamp"), cwd


def codex_messages(path):
    """(role, text, ts, cwd) for a Codex rollout file."""
    cwd = None
    for obj in _jsonl(path):
        if not isinstance(obj, dict):
            continue
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        if obj.get("type") in ("session_meta", "turn_context"):
            cwd = payload.get("cwd") or cwd
            continue
        if obj.get("type") != "response_item" or payload.get("type") != "message":
            continue
        role, content = payload.get("role"), payload.get("content")
        text = " ".join(b.get("text", "") for b in content if isinstance(b, dict)).strip() \
            if isinstance(content, list) else str(content or "").strip()
        if role in ("user", "assistant") and text:
            yield role, text, obj.get("timestamp"), cwd


READERS = {
    "claude_code": lambda path: thread_messages(path, "claude_code"),
    "pi": lambda path: thread_messages(path, "pi"),
    "codex": codex_messages,
}


def parse_ts(ts):
    dt = datetime.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=datetime.timezone.utc)).astimezone(datetime.timezone.utc)


def pairs_from(messages, platform):
    """(last agent turn, next human reply) pairs; consecutive agent turns keep only the last.
    Scrub the full text first, then truncate, so a key on the boundary cannot leave a tail."""
    agent, out = None, []
    for role, text, ts, cwd in messages:
        if role == "assistant":
            agent = text
        elif agent and looks_like_human_reply(text):
            if ts:  # untimestamped turns cannot be ordered or split at the cutoff
                out.append({"ts": parse_ts(ts).isoformat(), "platform": platform, "cwd": cwd,
                            "agent_turn": scrub(agent)[-TURN_CHARS:],
                            "user_reply": scrub(text)[:REPLY_CHARS]})
            agent = None
    return out


def collect(sources, max_pairs):
    """Deduplicated pairs (oldest first, the newest `max_pairs` kept) and per-platform counts.
    A store directory that does not exist counts as zero."""
    seen, rows = set(), []
    for platform, root in sources.items():
        if platform not in READERS:
            raise ValueError(f"brainprint: unknown session source '{platform}'; "
                             f"expected one of {sorted(READERS)}")
        root = Path(root)
        if not root.exists():
            continue
        if not root.is_dir():
            raise NotADirectoryError(f"brainprint: session source '{platform}' is not a directory: {root}")
        for path in sorted(root.rglob("*.jsonl")):
            for p in pairs_from(READERS[platform](path), platform):
                key = (p["ts"], p["user_reply"][:200])
                if key not in seen:
                    seen.add(key)
                    rows.append(p)
    rows.sort(key=lambda p: p["ts"])
    rows = rows[-max_pairs:] if max_pairs > 0 else []
    return rows, count_by_platform(rows, sources)


def count_by_platform(rows, sources):
    return {name: sum(r["platform"] == name for r in rows) for name in sources}


def short_hash(text):
    """Stable 16-hex-char content key for notes, pair ids and page versions."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def write_atomic(path, text):
    """Write text atomically: temp file in the same directory, then os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def write_json(path, obj, ensure_ascii=True):
    write_atomic(path, json.dumps(obj, indent=2, ensure_ascii=ensure_ascii) + "\n")


def write_pairs(path, rows):
    """Write rows as JSONL atomically."""
    write_atomic(path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))


def read_pairs(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def build(cfg):
    """Collect pairs from cfg's session sources and write <private_dir>/pairs.jsonl."""
    rows, counts = collect(cfg["session_sources"], cfg["max_pairs"])
    write_pairs(Path(cfg["private_dir"]) / PAIRS_FILE, rows)
    return rows, counts


def split(rows, cutoff):
    """(before, after) the cutoff date: a pair on the cutoff day or later is held out."""
    before = [r for r in rows if parse_ts(r["ts"]).date() < cutoff]
    after = [r for r in rows if parse_ts(r["ts"]).date() >= cutoff]
    return before, after
