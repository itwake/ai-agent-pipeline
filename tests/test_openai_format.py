import unittest

from pipeline_agent.providers.base import ProviderError
from pipeline_agent.providers.openai_format import (
    chat_messages,
    chat_tools,
    normalize_call_id,
    parse_reply,
    responses_input,
    responses_tools,
)
from pipeline_agent.session import Message, ToolCall
from pipeline_agent.tools.base import ToolSpec


def _conversation():
    return [
        Message(role="user", content="do it"),
        Message(role="assistant", content="ok", tool_calls=[ToolCall(id="call_1", name="bash", arguments={"command": "ls"}, arguments_text='{"command":"ls"}')]),
        Message(role="tool", content="file.txt", tool_call_id="call_1", name="bash"),
        Message(role="assistant", content="", tool_calls=[ToolCall(id="call_2", name="read_file", arguments={"path": "file.txt"})]),
        Message(role="tool", content="hello", tool_call_id="call_2", name="read_file"),
    ]


class ProjectionTests(unittest.TestCase):
    def test_responses_input_shape(self):
        items = responses_input(_conversation())
        self.assertEqual(items[0], {"role": "user", "content": [{"type": "input_text", "text": "do it"}]})
        self.assertEqual(items[1]["role"], "assistant")
        self.assertEqual(items[1]["content"][0]["type"], "output_text")
        self.assertEqual(items[2], {"type": "function_call", "call_id": "call_1", "name": "bash", "arguments": '{"command":"ls"}'})
        self.assertEqual(items[3], {"type": "function_call_output", "call_id": "call_1", "output": "file.txt"})
        # An assistant turn with no text produces only the function_call item.
        self.assertEqual(items[4]["type"], "function_call")
        self.assertEqual(items[4]["arguments"], '{"path":"file.txt"}')
        self.assertEqual(len(items), 6)

    def test_chat_messages_shape(self):
        messages = chat_messages("sys", _conversation())
        self.assertEqual(messages[0], {"role": "system", "content": "sys"})
        self.assertEqual(messages[1], {"role": "user", "content": "do it"})
        self.assertEqual(messages[2]["tool_calls"][0]["function"]["name"], "bash")
        self.assertEqual(messages[3], {"role": "tool", "tool_call_id": "call_1", "content": "file.txt", "name": "bash"})
        self.assertIsNone(messages[4]["content"])

    def test_tool_schemas(self):
        spec = ToolSpec(name="bash", description="run", parameters={"type": "object"})
        self.assertEqual(responses_tools([spec])[0], {"type": "function", "name": "bash", "description": "run", "parameters": {"type": "object"}})
        self.assertEqual(chat_tools([spec])[0]["function"]["name"], "bash")

    def test_long_call_ids_are_hashed(self):
        self.assertEqual(normalize_call_id("short"), "short")
        long_id = "x" * 100
        normalized = normalize_call_id(long_id)
        self.assertEqual(len(normalized), 64)
        self.assertTrue(normalized.startswith("call_"))


class ParseTests(unittest.TestCase):
    def test_responses_reply(self):
        raw = {
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": []},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Running"}]},
                {"type": "function_call", "call_id": "call_abc", "name": "bash", "arguments": '{"command": "ls"}'},
            ],
            "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
        }
        reply = parse_reply(raw)
        self.assertEqual(reply.text, "Running")
        self.assertEqual(len(reply.tool_calls), 1)
        self.assertEqual(reply.tool_calls[0].id, "call_abc")
        self.assertEqual(reply.tool_calls[0].arguments, {"command": "ls"})
        self.assertEqual(reply.usage, {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15})
        self.assertEqual(reply.finish_reason, "tool_calls")

    def test_chat_reply(self):
        raw = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "done",
                        "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "x"}'}}],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        }
        reply = parse_reply(raw)
        self.assertEqual(reply.text, "done")
        self.assertEqual(reply.tool_calls[0].name, "read_file")
        self.assertEqual(reply.usage["input_tokens"], 5)
        self.assertEqual(reply.usage["output_tokens"], 7)

    def test_invalid_arguments_are_flagged(self):
        raw = {"output": [{"type": "function_call", "call_id": "c", "name": "bash", "arguments": "{not json"}]}
        reply = parse_reply(raw)
        self.assertIsNone(reply.tool_calls[0].arguments)
        self.assertEqual(reply.tool_calls[0].arguments_text, "{not json")

    def test_incomplete_length(self):
        raw = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "output": []}
        self.assertEqual(parse_reply(raw).finish_reason, "length")

    def test_error_raises(self):
        with self.assertRaises(ProviderError):
            parse_reply({"error": {"message": "bad request", "code": "invalid_request"}})


if __name__ == "__main__":
    unittest.main()
