import unittest

from pipeline_agent.redaction import MASK, Redactor


class RedactorTests(unittest.TestCase):
    def test_known_secret_is_masked(self):
        redactor = Redactor(["s3cr3t-value"])
        self.assertEqual(redactor.redact("token=s3cr3t-value ok"), f"token={MASK} ok")

    def test_short_values_are_ignored(self):
        redactor = Redactor(["abc"])
        self.assertEqual(redactor.redact("abc"), "abc")

    def test_longest_secret_wins(self):
        redactor = Redactor(["abcdef", "abcdef-longer"])
        self.assertEqual(redactor.redact("abcdef-longer"), MASK)

    def test_github_token_shapes(self):
        redactor = Redactor()
        text = "export T=ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ123456 and gho_abcdefghijklmnopqrstuvwxyz0123"
        masked = redactor.redact(text)
        self.assertNotIn("ghp_ABCDEF", masked)
        self.assertNotIn("gho_abcdef", masked)
        self.assertEqual(masked.count(MASK), 2)

    def test_bearer_header_and_url_credentials(self):
        redactor = Redactor()
        self.assertEqual(redactor.redact("Authorization: Bearer abc.def.ghi"), f"Authorization: Bearer {MASK}")
        self.assertEqual(redactor.redact("https://user:pa55word@github.example.com/x.git"), f"https://user:{MASK}@github.example.com/x.git")

    def test_none_and_empty(self):
        redactor = Redactor(["something-secret"])
        self.assertEqual(redactor.redact(None), "")
        self.assertEqual(redactor.redact(""), "")

    def test_added_later(self):
        redactor = Redactor()
        redactor.add("later-secret-value")
        self.assertEqual(redactor.redact("later-secret-value"), MASK)


if __name__ == "__main__":
    unittest.main()
