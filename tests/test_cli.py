"""End-to-end tests of the command line against the scripted provider."""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from pipeline_agent.cli import main
from pipeline_agent.state import read_properties, read_state


class CliTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="agent-cli-"))
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.state_dir = self.root / "state"
        self.skills = self.root / "skills"
        (self.skills / "demo").mkdir(parents=True)
        (self.skills / "demo" / "skill.md").write_text("---\nname: demo\ndescription: Demo skill\n---\nAlways say DEMO-MARKER.\n", encoding="utf-8")
        self.script = self.root / "script.json"
        self.script.write_text(
            json.dumps(
                [
                    {"text": "Checking", "tool_calls": [{"name": "bash", "arguments": {"command": "echo marker-from-bash"}}]},
                    {"tool_calls": [{"name": "ask_user", "arguments": {"questions": [{"question": "Proceed?", "header": "Go", "options": [{"label": "yes"}, {"label": "no"}]}]}}]},
                    {"text": "## Summary\nFinished after the user said yes."},
                ]
            ),
            encoding="utf-8",
        )
        self._saved_environ = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved_environ)
        shutil.rmtree(self.root, ignore_errors=True)

    def _common(self, *extra: str) -> list[str]:
        return [
            "--provider", "fake", "--fake-script", str(self.script),
            "--workspace", str(self.workspace), "--state-dir", str(self.state_dir),
            "--session-id", "build-1", "--skills-dir", str(self.skills), "--skill", "demo",
            *extra,
        ]

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_run_question_resume_cycle(self):
        code, out, _ = self._run(["run", "--prompt", "do the thing", *self._common()])
        self.assertEqual(code, 10, out)
        props = read_properties(self.state_dir / "state.properties")
        self.assertEqual(props["STATUS"], "waiting_for_question")
        self.assertEqual(props["QUESTION_COUNT"], "1")
        self.assertEqual(props["Q0_TEXT"], "Proceed?")
        self.assertEqual(props["Q0_OPT0_LABEL"], "yes")
        self.assertIn("marker-from-bash", out)
        self.assertIn("the build will pause", out)

        code, out, _ = self._run(["state", "--state-dir", str(self.state_dir), "--field", "STATUS"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "waiting_for_question")

        answers = self.state_dir / "answers.properties"
        answers.write_text("Q0=yes\n", encoding="utf-8")
        code, out, _ = self._run(["resume", "--answers-file", str(answers), *self._common()])
        self.assertEqual(code, 0, out)
        state = read_state(self.state_dir)
        self.assertEqual(state["status"], "completed")
        self.assertIn("Finished after the user said yes", (self.state_dir / "final_message.md").read_text(encoding="utf-8"))
        self.assertEqual(state["usage"]["model_calls"], 3)
        self.assertTrue((self.state_dir / "sessions" / "build-1.json").exists())
        self.assertTrue((self.state_dir / "sessions" / "build-1.transcript.jsonl").exists())

    def test_resume_without_session_is_usage_error(self):
        code, _, err = self._run(["resume", *self._common()])
        self.assertEqual(code, 2)
        self.assertIn("no session", err)
        self.assertEqual(read_state(self.state_dir)["status"], "error")

    def test_unknown_skill_fails_fast(self):
        code, _, err = self._run(["run", "--prompt", "x", *self._common("--skill", "missing")])
        self.assertEqual(code, 2)
        self.assertIn("missing", err)
        self.assertIn("demo", err)

    def test_prompt_and_skills_commands(self):
        code, out, _ = self._run(["prompt", *self._common()])
        self.assertEqual(code, 0)
        self.assertIn("DEMO-MARKER", out)
        self.assertIn("# Final report", out)
        code, out, _ = self._run(["skills", "--skills-dir", str(self.skills)])
        self.assertEqual(code, 0)
        self.assertIn("demo\tDemo skill", out)

    def test_check_and_probe(self):
        self.script.write_text(json.dumps([{"text": "ok"}]), encoding="utf-8")
        code, out, _ = self._run(["check", "--probe", *self._common()])
        self.assertEqual(code, 0, out)
        self.assertIn("probe reply: 'ok'", out)

    def test_dry_run_blocks_push(self):
        self.script.write_text(json.dumps([{"tool_calls": [{"name": "bash", "arguments": {"command": "git push origin x"}}]}, {"text": "done"}]), encoding="utf-8")
        code, out, _ = self._run(["run", "--prompt", "push it", "--dry-run", *self._common()])
        self.assertEqual(code, 0)
        self.assertIn("dry run", out)

    def test_unknown_model_rejected(self):
        code, _, err = self._run(["run", "--prompt", "x", *self._common("--model", "gpt-unknown")])
        self.assertEqual(code, 2)
        self.assertIn("not available", err)

    def test_secret_env_is_masked_and_hidden(self):
        os.environ["COPILOT_GITHUB_TOKEN"] = "ghp_thisIsASecretTokenValue000000"
        os.environ["GIT_PASSWORD"] = "git-password-value"
        self.script.write_text(
            json.dumps([{"tool_calls": [{"name": "bash", "arguments": {"command": "echo ${COPILOT_GITHUB_TOKEN:-hidden} && echo $GIT_PASSWORD"}}]}, {"text": "done"}]),
            encoding="utf-8",
        )
        code, out, _ = self._run(["run", "--prompt", "leak", *self._common()])
        self.assertEqual(code, 0)
        self.assertNotIn("thisIsASecretTokenValue", out)
        self.assertNotIn("git-password-value", out)
        self.assertIn("hidden", out)
        session = json.loads((self.state_dir / "sessions" / "build-1.json").read_text(encoding="utf-8"))
        tool_output = [m for m in session["messages"] if m["role"] == "tool"][0]["content"]
        self.assertNotIn("git-password-value", tool_output)
        self.assertIn("***", tool_output)


if __name__ == "__main__":
    unittest.main()
