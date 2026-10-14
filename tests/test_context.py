import unittest

from pipeline_agent.context import SUMMARY_MARKER, estimate_request_tokens
from pipeline_agent.session import Message, Session, ToolCall

from .helpers import Harness


def _long_session(turns: int, output_chars: int) -> Session:
    session = Session(session_id="ctx")
    session.messages.append(Message(role="user", content="fix the build"))
    for index in range(turns):
        call = ToolCall(id=f"call_{index}", name="bash", arguments={"command": f"cmd {index}"}, arguments_text='{"command":"x"}')
        session.messages.append(Message(role="assistant", content=f"step {index}", tool_calls=[call]))
        session.messages.append(Message(role="tool", content=("o" * output_chars) + f" end {index}", tool_call_id=call.id, name="bash", meta={"archive": f"/tmp/{index}.txt"}))
    return session


class ContextTests(unittest.TestCase):
    def test_under_budget_is_untouched(self):
        harness = Harness(budget_tokens=1_000_000)
        try:
            session = _long_session(3, 100)
            before = [m.content for m in session.messages]
            harness.context.prepare(session, "sys", harness.registry.specs())
            self.assertEqual([m.content for m in session.messages], before)
        finally:
            harness.cleanup()

    def test_pruning_old_tool_output(self):
        # 10 turns of 8k chars = ~20k tokens; budget 12k forces pruning of old outputs.
        harness = Harness(budget_tokens=12_000, keep_recent=4)
        try:
            session = _long_session(10, 8_000)
            harness.context.prepare(session, "sys", harness.registry.specs())
            old_tool = session.messages[2]
            self.assertTrue(old_tool.meta.get("pruned"))
            self.assertIn("cut to save context", old_tool.content)
            self.assertIn("/tmp/0.txt", old_tool.content)
            recent_tool = session.messages[-1]
            self.assertFalse(recent_tool.meta.get("pruned", False))
            self.assertLessEqual(estimate_request_tokens("sys", session.messages, harness.registry.specs()), 12_000)
        finally:
            harness.cleanup()

    def test_summarization_replaces_middle(self):
        harness = Harness(script=[{"text": "## Task\nfix the build\n## Progress\nsteps 0-5 done"}], budget_tokens=3_000, keep_recent=4)
        try:
            session = _long_session(10, 3_000)
            harness.context.prepare(session, "sys", harness.registry.specs())
            self.assertEqual(session.compactions, 1)
            self.assertEqual(session.messages[0].content, "fix the build")
            self.assertTrue(session.messages[1].content.startswith(SUMMARY_MARKER))
            self.assertIn("steps 0-5 done", session.messages[1].content)
            # The tail keeps complete assistant/tool pairs.
            self.assertEqual(session.messages[2].role, "assistant")
            self.assertEqual(session.messages[-1].role, "tool")
            self.assertEqual(session.usage["model_calls"], 1)
        finally:
            harness.cleanup()

    def test_summary_failure_falls_back_to_pruning(self):
        harness = Harness(script=[{"text": ""}], budget_tokens=3_000, keep_recent=4)
        try:
            session = _long_session(10, 3_000)
            harness.context.prepare(session, "sys", harness.registry.specs())
            self.assertEqual(session.compactions, 0)
            self.assertTrue(all(m.meta.get("pruned") for m in session.messages if m.role == "tool"))
        finally:
            harness.cleanup()

    def test_compact_with_too_little_history(self):
        harness = Harness(budget_tokens=100)
        try:
            session = _long_session(1, 50)
            changed = harness.context.compact(session, reason="test")
            self.assertFalse(changed)
        finally:
            harness.cleanup()


if __name__ == "__main__":
    unittest.main()
