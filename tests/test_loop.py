import unittest

from pipeline_agent.loop import STATUS_COMPLETED, STATUS_ERROR, STATUS_MAX_ITERATIONS, STATUS_WAITING_FOR_QUESTION
from pipeline_agent.providers.base import ProviderError, TransientProviderError
from pipeline_agent.session import Message

from .helpers import Harness, ask_call, bash_call


class LoopTests(unittest.TestCase):
    def test_text_only_reply_completes(self):
        harness = Harness([{"text": "All done."}])
        try:
            outcome = harness.loop.start("s1", "hello")
            self.assertEqual(outcome.status, STATUS_COMPLETED)
            self.assertEqual(outcome.final_text, "All done.")
            self.assertEqual(outcome.session.iterations, 1)
            self.assertTrue(harness.store.exists("s1"))
            self.assertTrue(harness.store.transcript_path("s1").exists())
        finally:
            harness.cleanup()

    def test_tool_call_round_trip(self):
        harness = Harness([{"text": "Listing", "tool_calls": [bash_call("echo from-bash")]}, {"text": "Finished."}])
        try:
            outcome = harness.loop.start("s2", "list")
            self.assertEqual(outcome.status, STATUS_COMPLETED)
            session = outcome.session
            self.assertEqual([m.role for m in session.messages], ["user", "assistant", "tool", "assistant"])
            self.assertIn("from-bash", session.messages[2].content)
            # The second model request carried the tool result.
            second_request = harness.provider.requests[1]
            self.assertEqual(second_request["messages"][2]["role"], "tool")
            self.assertIn("from-bash", harness.console_text)
        finally:
            harness.cleanup()

    def test_question_pauses_and_resume_continues(self):
        harness = Harness(
            [
                {"text": "Need input", "tool_calls": [ask_call({"question": "Which branch?", "header": "Branch", "options": [{"label": "main"}, {"label": "dev"}]})]},
                {"text": "Using main. Done."},
            ]
        )
        try:
            outcome = harness.loop.start("s3", "task")
            self.assertEqual(outcome.status, STATUS_WAITING_FOR_QUESTION)
            self.assertEqual(outcome.questions[0]["question"], "Which branch?")
            stored = harness.store.load("s3")
            self.assertIsNotNone(stored.pending_question)
            # Resuming without answers stays parked.
            parked = harness.loop.resume("s3")
            self.assertEqual(parked.status, STATUS_WAITING_FOR_QUESTION)
            resumed = harness.loop.resume("s3", answers={0: "main"})
            self.assertEqual(resumed.status, STATUS_COMPLETED)
            session = resumed.session
            self.assertIsNone(session.pending_question)
            tool_message = [m for m in session.messages if m.role == "tool"][0]
            self.assertEqual(tool_message.name, "ask_user")
            self.assertIn("Answer: main", tool_message.content)
            self.assertEqual(resumed.final_text, "Using main. Done.")
        finally:
            harness.cleanup()

    def test_question_after_other_calls_in_same_turn(self):
        harness = Harness(
            [
                {"tool_calls": [bash_call("echo first"), ask_call({"question": "Continue?"}), bash_call("echo third")]},
                {"text": "ok"},
            ]
        )
        try:
            outcome = harness.loop.start("s4", "task")
            self.assertEqual(outcome.status, STATUS_WAITING_FOR_QUESTION)
            roles = [m.role for m in outcome.session.messages]
            self.assertEqual(roles, ["user", "assistant", "tool"])  # only the first bash ran
            resumed = harness.loop.resume("s4", answers={0: "yes"})
            self.assertEqual(resumed.status, STATUS_COMPLETED)
            tools = [m for m in resumed.session.messages if m.role == "tool"]
            self.assertEqual([m.name for m in tools], ["bash", "ask_user", "bash"])
            self.assertIn("third", tools[2].content)
        finally:
            harness.cleanup()

    def test_iteration_limit_and_extension(self):
        harness = Harness([{"tool_calls": [bash_call("echo a")]}, {"tool_calls": [bash_call("echo b")]}, {"text": "done"}], max_iterations=2)
        try:
            outcome = harness.loop.start("s5", "task")
            self.assertEqual(outcome.status, STATUS_MAX_ITERATIONS)
            self.assertEqual(outcome.session.iterations, 2)
            resumed = harness.loop.resume("s5", more_iterations=5)
            self.assertEqual(resumed.status, STATUS_COMPLETED)
            self.assertEqual(resumed.session.iterations, 3)
        finally:
            harness.cleanup()

    def test_doom_loop_is_blocked(self):
        repeated = bash_call("echo same")
        harness = Harness([{"tool_calls": [repeated]}, {"tool_calls": [repeated]}, {"tool_calls": [repeated]}, {"tool_calls": [repeated]}, {"text": "giving up"}])
        try:
            outcome = harness.loop.start("s6", "task")
            self.assertEqual(outcome.status, STATUS_COMPLETED)
            tools = [m for m in outcome.session.messages if m.role == "tool"]
            self.assertEqual(len(tools), 4)
            self.assertIn("same", tools[2].content)
            self.assertIn("already been made", tools[3].content)
        finally:
            harness.cleanup()

    def test_unknown_tool_and_bad_arguments(self):
        harness = Harness([{"tool_calls": [{"name": "nope", "arguments": {}}, {"name": "bash", "arguments": "not-json"}]}, {"text": "ok"}])
        try:
            outcome = harness.loop.start("s7", "task")
            tools = [m for m in outcome.session.messages if m.role == "tool"]
            self.assertIn("Unknown tool", tools[0].content)
            self.assertIn("not a valid JSON object", tools[1].content)
        finally:
            harness.cleanup()

    def test_provider_error_marks_session(self):
        harness = Harness()

        class Failing:
            name = "fake"
            model = "fake"

            def complete(self, **kwargs):
                raise ProviderError("boom")

        harness.loop.provider = Failing()
        try:
            outcome = harness.loop.start("s8", "task")
            self.assertEqual(outcome.status, STATUS_ERROR)
            self.assertIn("boom", outcome.error)
            self.assertEqual(harness.store.load("s8").status, STATUS_ERROR)
        finally:
            harness.cleanup()

    def test_transient_errors_are_retried(self):
        harness = Harness([{"text": "after retry"}])
        harness.loop.settings.retry_backoff_seconds = 0
        real = harness.provider
        calls = {"n": 0}

        class Flaky:
            name = "fake"
            model = "fake"

            def complete(self, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise TransientProviderError("429")
                return real.complete(**kwargs)

        harness.loop.provider = Flaky()
        try:
            outcome = harness.loop.start("s9", "task")
            self.assertEqual(outcome.status, STATUS_COMPLETED)
            self.assertEqual(calls["n"], 2)
        finally:
            harness.cleanup()

    def test_resume_reruns_unfinished_tool_calls(self):
        harness = Harness([{"text": "ok"}])
        try:
            session = harness.loop.start("s10", "task").session
            # Simulate a crash after the model asked for a tool but before it ran.
            session.messages.append(Message(role="assistant", content="", tool_calls=[]))
            session.messages[-1].tool_calls.append(__import__("pipeline_agent.session", fromlist=["ToolCall"]).ToolCall(id="c-late", name="bash", arguments={"command": "echo recovered"}))
            harness.store.save(session)
            harness.provider._script.append({"text": "finished"})
            resumed = harness.loop.resume("s10")
            self.assertEqual(resumed.status, STATUS_COMPLETED)
            self.assertIn("recovered", [m for m in resumed.session.messages if m.role == "tool"][0].content)
        finally:
            harness.cleanup()


if __name__ == "__main__":
    unittest.main()
