import tempfile
import unittest
from pathlib import Path

from pipeline_agent.tools.permissions import CommandPolicy


class CommandPolicyTests(unittest.TestCase):
    def test_defaults_block_destructive_commands(self):
        policy = CommandPolicy.build()
        self.assertIsNotNone(policy.check("rm -rf /"))
        self.assertIsNotNone(policy.check("git push --force origin main"))
        self.assertIsNotNone(policy.check("mvn clean deploy"))
        self.assertIsNotNone(policy.check("sudo apt install x"))
        self.assertIsNotNone(policy.check("kubectl delete pod x"))

    def test_defaults_allow_ordinary_work(self):
        policy = CommandPolicy.build()
        for command in ("git status", "mvn -B -q package", "git push origin feature/x", "ls -la", "echo hi", "rm -rf target/"):
            self.assertIsNone(policy.check(command), command)

    def test_dry_run_blocks_push(self):
        policy = CommandPolicy.build(dry_run=True)
        reason = policy.check("git push origin feature/x")
        self.assertIsNotNone(reason)
        self.assertIn("dry run", reason)
        self.assertIsNone(policy.check("git commit -m x"))

    def test_extra_and_file_patterns(self):
        with tempfile.TemporaryDirectory() as tmp:
            deny_file = Path(tmp) / "deny.txt"
            deny_file.write_text("# comment\n*curl *internal*\n", encoding="utf-8")
            policy = CommandPolicy.build(deny_file=deny_file, extra_patterns=["*drop table*"])
            self.assertIsNotNone(policy.check("curl http://internal/x"))
            self.assertIsNotNone(policy.check("psql -c 'DROP TABLE x'"))
            self.assertIsNone(policy.check("curl https://example.com"))

    def test_whitespace_and_case_are_normalized(self):
        policy = CommandPolicy.build()
        self.assertIsNotNone(policy.check("  GIT   push   --FORCE origin x"))


if __name__ == "__main__":
    unittest.main()
