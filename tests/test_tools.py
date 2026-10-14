import unittest

from pipeline_agent.tools.ask_user import AskUserTool, format_answers, normalize_questions
from pipeline_agent.tools.base import ToolError
from pipeline_agent.tools.bash import BashTool, truncate_middle
from pipeline_agent.tools.files import EditFileTool, ReadFileTool, WriteFileTool

from .helpers import Harness


class BashToolTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.ctx = self.harness.tool_context("s1", "call-1")

    def tearDown(self):
        self.harness.cleanup()

    def test_runs_and_reports_exit_code(self):
        result = BashTool().run({"command": "echo hello && echo oops 1>&2 && exit 3"}, self.ctx)
        self.assertFalse(result.success)
        self.assertEqual(result.meta["exit_code"], 3)
        self.assertIn("<stdout>\nhello", result.content)
        self.assertIn("<stderr>\noops", result.content)
        self.assertIn("[exit code: 3", result.content)

    def test_archives_full_output(self):
        result = BashTool().run({"command": "echo archived"}, self.ctx)
        archive = result.meta["archive"]
        with open(archive, encoding="utf-8") as handle:
            self.assertIn("archived", handle.read())

    def test_timeout_kills_the_command(self):
        result = BashTool().run({"command": "sleep 5; echo late", "timeout_seconds": 1}, self.ctx)
        self.assertFalse(result.success)
        self.assertTrue(result.meta["timed_out"])
        self.assertIn("timed out after 1s", result.content)
        self.assertNotIn("late", result.content)

    def test_timeout_is_clamped(self):
        result = BashTool().run({"command": "echo x", "timeout_seconds": 99999}, self.ctx)
        self.assertTrue(result.success)

    def test_blocked_by_policy(self):
        result = BashTool().run({"command": "git push --force origin main"}, self.ctx)
        self.assertFalse(result.success)
        self.assertTrue(result.meta["blocked"])
        self.assertIn("blocked", result.content)

    def test_workdir_must_stay_inside_workspace(self):
        with self.assertRaises(ToolError):
            BashTool().run({"command": "pwd", "workdir": "../.."}, self.ctx)

    def test_workdir_relative(self):
        (self.harness.workspace / "sub").mkdir()
        result = BashTool().run({"command": "pwd", "workdir": "sub"}, self.ctx)
        self.assertTrue(result.success)
        self.assertEqual(result.meta["cwd"], "sub")

    def test_secret_values_are_masked(self):
        self.harness.redactor.add("super-secret-token")
        result = BashTool().run({"command": "echo super-secret-token"}, self.ctx)
        self.assertNotIn("super-secret-token", result.content)
        self.assertIn("***", result.content)

    def test_missing_command(self):
        with self.assertRaises(ToolError):
            BashTool().run({}, self.ctx)


class TruncateTests(unittest.TestCase):
    def test_short_text_untouched(self):
        text, truncated = truncate_middle("abc", 100, 10)
        self.assertEqual(text, "abc")
        self.assertFalse(truncated)

    def test_long_text_keeps_head_and_tail(self):
        lines = [f"line {i}" for i in range(1000)]
        text, truncated = truncate_middle("\n".join(lines), 2000, 40)
        self.assertTrue(truncated)
        self.assertIn("line 0", text)
        self.assertIn("line 999", text)
        self.assertIn("omitted", text)
        self.assertLessEqual(len(text), 2000 + 100)


class FileToolsTests(unittest.TestCase):
    def setUp(self):
        self.harness = Harness()
        self.ctx = self.harness.tool_context("s1", "call-2")

    def tearDown(self):
        self.harness.cleanup()

    def test_write_read_edit(self):
        write = WriteFileTool().run({"path": "src/a.txt", "content": "one\ntwo\nthree\n"}, self.ctx)
        self.assertIn("Created", write.content)
        read = ReadFileTool().run({"path": "src/a.txt"}, self.ctx)
        self.assertIn("1| one", read.content)
        self.assertIn("3| three", read.content)
        sliced = ReadFileTool().run({"path": "src/a.txt", "start_line": 2, "end_line": 2}, self.ctx)
        self.assertIn("2| two", sliced.content)
        self.assertNotIn("one", sliced.content)
        edit = EditFileTool().run({"path": "src/a.txt", "old_string": "two", "new_string": "2"}, self.ctx)
        self.assertTrue(edit.success)
        self.assertEqual((self.harness.workspace / "src" / "a.txt").read_text(encoding="utf-8"), "one\n2\nthree\n")

    def test_edit_requires_unique_match(self):
        WriteFileTool().run({"path": "b.txt", "content": "x x x"}, self.ctx)
        result = EditFileTool().run({"path": "b.txt", "old_string": "x", "new_string": "y"}, self.ctx)
        self.assertFalse(result.success)
        self.assertIn("3 times", result.content)
        result = EditFileTool().run({"path": "b.txt", "old_string": "x", "new_string": "y", "replace_all": True}, self.ctx)
        self.assertTrue(result.success)
        self.assertEqual((self.harness.workspace / "b.txt").read_text(encoding="utf-8"), "y y y")

    def test_edit_missing_text(self):
        WriteFileTool().run({"path": "c.txt", "content": "hello"}, self.ctx)
        result = EditFileTool().run({"path": "c.txt", "old_string": "nope", "new_string": "y"}, self.ctx)
        self.assertFalse(result.success)

    def test_read_directory_lists_entries(self):
        (self.harness.workspace / "d").mkdir()
        (self.harness.workspace / "d" / "f.txt").write_text("x", encoding="utf-8")
        result = ReadFileTool().run({"path": "d"}, self.ctx)
        self.assertIn("is a directory", result.content)
        self.assertIn("f.txt", result.content)

    def test_paths_cannot_escape(self):
        with self.assertRaises(ToolError):
            ReadFileTool().run({"path": "../outside.txt"}, self.ctx)
        with self.assertRaises(ToolError):
            WriteFileTool().run({"path": "/etc/passwd-test", "content": "x"}, self.ctx)


class AskUserTests(unittest.TestCase):
    def test_normalize_and_pause(self):
        harness = Harness()
        try:
            ctx = harness.tool_context("s", "c")
            result = AskUserTool().run(
                {"questions": [{"question": "Which branch?", "header": "Branch", "options": [{"label": "main"}, "develop"], "allow_custom": False}]},
                ctx,
            )
            self.assertEqual(result.pause, "question")
            questions = result.meta["questions"]
            self.assertEqual(questions[0]["options"][1]["label"], "develop")
            self.assertFalse(questions[0]["allow_custom"])
        finally:
            harness.cleanup()

    def test_invalid_questions(self):
        with self.assertRaises(ToolError):
            normalize_questions([])
        with self.assertRaises(ToolError):
            normalize_questions([{"header": "x"}])

    def test_format_answers(self):
        questions = normalize_questions([{"question": "A?"}, {"question": "B?"}])
        text = format_answers(questions, {0: "yes"})
        self.assertIn("1. A?", text)
        self.assertIn("Answer: yes", text)
        self.assertIn("Answer: (no answer given)", text)


if __name__ == "__main__":
    unittest.main()
