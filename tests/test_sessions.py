import datetime
import json
import tempfile
import unittest
from pathlib import Path

from pipeline import sessions

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sessions"


def fake_secrets():
    """(text to embed, secret part that must never survive). Built at runtime so no
    realistic key sits in a fixture file."""
    jwt = "eyJ" + "hbGciOiJIUzI1NiJ9" + "." + "eyJ" + "zdWIiOiIxMjM0NTY3ODkwIn0" + "." + "Sfl" + "KxwRJ" * 6
    return [
        ("sk-" + "A1b2" * 6, "A1b2" * 6),
        ("AKIA" + "ABCD1234EFGH5678", "ABCD1234EFGH5678"),
        ("ghp_" + "x9" * 18, "x9" * 18),
        ("github_pat_" + "11AB" * 6 + "_" + "cd" * 10, "11AB" * 6),
        ("xoxb-" + "1234-5678-" + "abcDEF" * 3, "abcDEF" * 3),
        ("AIza" + "Sy" + "Q7" * 17, "Q7" * 17),
        ("sk_live_" + "4eC3" * 6, "4eC3" * 6),
        ("rk_live_" + "9zY8" * 6, "9zY8" * 6),
        (jwt, "KxwRJ" * 6),
        ("Authorization: Bearer " + "abc123DEF" + "456ghi789", "abc123DEF456ghi789"),
        ("mysql://" + "admin:" + "hunter2secret" + "@db.example.com/app", "hunter2secret"),
        ("postgres://" + "u:" + "pgpass77" + "@h/db", "pgpass77"),
        ("PASSWORD=" + "correct" + "horse9", "correcthorse9"),
        ("DB_PASSWD=" + "stapler" + "42", "stapler42"),
        ("PRIVATE_KEY=" + "pk" + "material55", "pkmaterial55"),
        ("STRIPE_API_KEY=" + "shortish" + "value1", "shortishvalue1"),
        ('{"api_key": "' + "zq" * 10 + '"}', "zq" * 10),
        ("client_secret: '" + "yamlsec" + "ret88'", "yamlsecret88"),
        ('"password": "' + "pw" + "value99" + '"', "pwvalue99"),
        # Shapes the security review showed slipping through: unquoted, lowercase, Basic, npm, GitLab.
        ("password: " + "hunter2" + "secret", "hunter2secret"),
        ("api_key: " + "abcd1234" + "efgh", "abcd1234efgh"),
        ("export db_password=" + "Sup3r" + "S3cret!", "Sup3rS3cret!"),
        ("token=" + "abcdef" + "123456", "abcdef123456"),
        ("Authorization: Basic " + "dXNlcjpw" + "YXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
        ("npm_" + "Ab1" * 12, "Ab1" * 12),
        ("glpat-" + "Zx9_" * 6, "Zx9_" * 6),
        ("-----BEGIN RSA " + "PRIVATE KEY-----\n" + "MIIEow" + "Qm9keQ" * 5 + "\n-----END RSA PRIVATE KEY-----",
         "Qm9keQ" * 5),
    ]


def claude_line(role, text, ts, cwd="/w/s"):
    content = text if role == "user" else [{"type": "text", "text": text}]
    return json.dumps({"type": role, "isSidechain": False, "cwd": cwd, "timestamp": ts,
                       "message": {"role": role, "content": content}})


def pi_line(role, text, ts):
    return json.dumps({"type": "message", "timestamp": ts,
                       "message": {"role": role, "content": [{"type": "text", "text": text}]}})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.private = self.tmp / "private"

    def cfg(self, sources=None, max_pairs=100):
        if sources is None:
            sources = {name: FIXTURES / name for name in ("claude_code", "pi", "codex")}
        return {"session_sources": sources, "private_dir": self.private, "max_pairs": max_pairs}

    def written(self):
        return (self.private / "pairs.jsonl").read_text()


class FixturePairsTest(Base):
    def setUp(self):
        super().setUp()
        self.rows, self.counts = sessions.build(self.cfg())
        self.by_reply = {r["user_reply"]: r for r in self.rows}

    def test_each_platform_yields_expected_pairs_intact(self):
        self.assertEqual({"claude_code": 2, "pi": 1, "codex": 1}, self.counts)
        self.assertEqual(["wait for review", "merge it", "postgres", "keep the flag"],
                         [r["user_reply"] for r in self.rows])
        self.assertEqual({"ts": "2026-08-01T10:06:00+00:00", "platform": "claude_code", "cwd": "/w/proj",
                          "agent_turn": "Run the migration now or wait for review?",
                          "user_reply": "wait for review"}, self.by_reply["wait for review"])
        self.assertEqual("Review done. Merge?", self.by_reply["merge it"]["agent_turn"])
        self.assertEqual(("pi", "/w/pi", "Use sqlite or postgres?"),
                         tuple(self.by_reply["postgres"][k] for k in ("platform", "cwd", "agent_turn")))
        self.assertEqual(("codex", "/w/cx", "Should I keep the feature flag?"),
                         tuple(self.by_reply["keep the flag"][k] for k in ("platform", "cwd", "agent_turn")))

    def test_written_file_matches_returned_rows(self):
        lines = [json.loads(line) for line in self.written().splitlines()]
        self.assertEqual(self.rows, lines)
        self.assertEqual([], [p.name for p in self.private.iterdir() if p.name != "pairs.jsonl"])

    def test_non_human_turns_never_become_replies(self):
        text = self.written()
        for noise in ("tool output text", "system-reminder", "sidechain reply", "subagent question",
                      "tool result text", "environment_context", "function output text"):
            self.assertNotIn(noise, text)

    def test_only_last_of_consecutive_agent_turns_is_paired(self):
        self.assertNotIn("Draft plan ready", self.written())

    def test_resumed_session_reply_kept_once(self):
        self.assertEqual(1, [r["user_reply"] for r in self.rows].count("wait for review"))


class HumanReplyTest(unittest.TestCase):
    def test_old_format_continuation_summary_is_not_a_reply(self):
        text = "This session is being continued from a previous conversation that ran out of context."
        self.assertFalse(sessions.looks_like_human_reply(text))
        self.assertTrue(sessions.looks_like_human_reply("ship it"))


class VanishedFileTest(Base):
    def test_session_file_moved_after_listing_yields_nothing(self):
        self.assertEqual([], list(sessions.codex_messages(self.tmp / "rotated-away.jsonl")))


class ClaudeMetaEntriesTest(Base):
    def test_skill_bodies_and_compact_summaries_are_not_replies(self):
        path = self.tmp / "s.jsonl"
        lines = [claude_line("assistant", "Ship it or wait?", "2026-08-01T10:00:00Z"),
                 claude_line("user", "skill body text", "2026-08-01T10:01:00Z"),
                 claude_line("user", "compact summary text", "2026-08-01T10:02:00Z"),
                 claude_line("user", "ship it", "2026-08-01T10:03:00Z")]
        lines[1] = lines[1].replace('"isSidechain": false', '"isSidechain": false, "isMeta": true')
        lines[2] = lines[2].replace('"isSidechain": false', '"isSidechain": false, "isCompactSummary": true')
        path.write_text("\n".join(lines) + "\n")
        texts = [text for _, text, _, _ in sessions.thread_messages(path, "claude_code")]
        self.assertEqual(["Ship it or wait?", "ship it"], texts)


class ScrubTest(Base):
    def test_every_secret_family_redacted_in_file_and_return(self):
        secrets = fake_secrets()
        src = self.tmp / "cc" / "proj"
        src.mkdir(parents=True)
        lines = []
        for i, (embed, _) in enumerate(secrets):
            lines.append(claude_line("assistant", f"step {i}: using {embed} now, ok?", f"2026-08-01T10:{i:02d}:00Z"))
            lines.append(claude_line("user", f"reply {i} with {embed} ok", f"2026-08-01T10:{i:02d}:30Z"))
        (src / "s.jsonl").write_text("\n".join(lines) + "\n")
        rows, counts = sessions.build(self.cfg({"claude_code": self.tmp / "cc"}))
        self.assertEqual(len(secrets), counts["claude_code"])
        outputs = self.written() + json.dumps(rows)
        for embed, secret in secrets:
            with self.subTest(embed=embed[:12]):
                self.assertNotIn(secret, outputs)
        for r in rows:
            self.assertIn(sessions.REDACTED, r["agent_turn"])
            self.assertIn(sessions.REDACTED, r["user_reply"])

    def test_scrub_replaces_with_marker(self):
        for embed, secret in fake_secrets():
            with self.subTest(embed=embed[:12]):
                out = sessions.scrub(f"x {embed} y")
                self.assertNotIn(secret, out)
                self.assertIn("[REDACTED]", out)

    def test_key_straddling_truncation_boundary_is_redacted(self):
        key = "sk-" + "Zt" * 20
        agent = "a" * 100 + key + "b" * 1480
        reply = "c" * 990 + "ghp_" + "Rw" * 20
        [pair] = sessions.pairs_from([("assistant", agent, "2026-08-01T00:00:00Z", "/w"),
                                      ("user", reply, "2026-08-01T00:01:00Z", "/w")], "pi")
        self.assertLessEqual(len(pair["agent_turn"]), 1500)
        self.assertLessEqual(len(pair["user_reply"]), 1000)
        self.assertNotIn("ZtZtZt", pair["agent_turn"])
        self.assertNotIn("RwRw", pair["user_reply"])
        self.assertNotIn("ghp_", pair["user_reply"])
        self.assertIn("[REDACTED]", pair["agent_turn"])


class LimitsTest(Base):
    def test_max_pairs_keeps_newest(self):
        src = self.tmp / "pi" / "proj"
        src.mkdir(parents=True)
        lines = []
        for day in range(1, 6):
            lines.append(pi_line("assistant", f"question {day}?", f"2026-08-0{day}T09:00:00Z"))
            lines.append(pi_line("user", f"answer {day}", f"2026-08-0{day}T09:01:00Z"))
        (src / "s.jsonl").write_text("\n".join(lines) + "\n")
        rows, counts = sessions.build(self.cfg({"pi": self.tmp / "pi"}, max_pairs=2))
        self.assertEqual(["answer 4", "answer 5"], [r["user_reply"] for r in rows])
        self.assertEqual({"pi": 2}, counts)
        self.assertEqual(2, len(self.written().splitlines()))

    def test_missing_store_is_zero_not_error(self):
        sources = {"claude_code": FIXTURES / "claude_code", "codex": self.tmp / "nope", "pi": self.tmp / "nada"}
        rows, counts = sessions.build(self.cfg(sources))
        self.assertEqual({"claude_code": 2, "codex": 0, "pi": 0}, counts)
        self.assertEqual(2, len(rows))

    def test_split_at_cutoff(self):
        rows, _ = sessions.build(self.cfg())
        before, after = sessions.split(rows, datetime.date(2026, 8, 2))
        self.assertEqual(["wait for review", "merge it"], [r["user_reply"] for r in before])
        self.assertEqual(["postgres", "keep the flag"], [r["user_reply"] for r in after])

    def test_read_pairs_round_trips(self):
        rows, _ = sessions.build(self.cfg())
        self.assertEqual(rows, sessions.read_pairs(self.private / "pairs.jsonl"))


if __name__ == "__main__":
    unittest.main()
