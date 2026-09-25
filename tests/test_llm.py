import os
import unittest

from pipeline import llm


class LlmTest(unittest.TestCase):
    def test_run_echoes_prompt(self):
        self.assertEqual(llm.run("cat", "hello brain"), "hello brain")

    def test_runs_outside_caller_cwd_with_advisory_off(self):
        out = llm.run("sh -c 'cat >/dev/null; pwd; echo \"$BRAINPRINT_ADVISORY\"'", "x")
        cwd, advisory = out.splitlines()
        self.assertNotEqual(os.path.realpath(cwd), os.path.realpath(os.getcwd()))
        self.assertEqual(advisory, "0")

    def test_nonzero_exit_raises(self):
        with self.assertRaisesRegex(llm.LLMError, "exited 3"):
            llm.run("sh -c 'cat >/dev/null; echo boom >&2; exit 3'", "x")

    def test_empty_output_raises(self):
        with self.assertRaisesRegex(llm.LLMError, "empty output"):
            llm.run("sh -c 'cat >/dev/null'", "x")

    def test_timeout_raises_llm_error(self):
        with self.assertRaisesRegex(llm.LLMError, "timed out after 1s"):
            llm.run("sleep 5", "x", timeout=1)

    def test_runner_builds_callable(self):
        self.assertEqual(llm.runner("cat")("p"), "p")


if __name__ == "__main__":
    unittest.main()
