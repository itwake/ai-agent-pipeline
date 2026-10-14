"""Provider tests against a local fake HTTP server (no network)."""

from __future__ import annotations

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from pipeline_agent.providers.ai_platform import AIPlatformProvider, AIPlatformSettings, is_responses_endpoint
from pipeline_agent.providers.base import AuthenticationError, ContextOverflowError, TransientProviderError
from pipeline_agent.providers.copilot import CopilotProvider, api_base_from_token, is_source_token
from pipeline_agent.providers.http import HttpClient
from pipeline_agent.redaction import Redactor
from pipeline_agent.session import Message
from pipeline_agent.tools.base import ToolSpec


class FakeServer:
    """Routes (method, path) to handler callables; records every request."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Callable[[dict[str, Any]], tuple[int, dict[str, Any]]]] = {}
        self.requests: list[dict[str, Any]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                pass

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw.decode("utf-8")) if raw else None
                # urllib re-cases header names on the wire; compare case-insensitively.
                record = {"method": method, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body}
                server.requests.append(record)
                handler = server.routes.get((method, self.path.split("?")[0]))
                if handler is None:
                    status, payload = 404, {"error": {"message": "no route"}}
                else:
                    status, payload = handler(record)
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _responses_reply(text: str = "ok") -> dict[str, Any]:
    return {
        "status": "completed",
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 3, "output_tokens": 1},
    }


def _chat_reply(text: str = "ok") -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 3, "completion_tokens": 1}}


TOOL = ToolSpec(name="bash", description="run", parameters={"type": "object", "properties": {"command": {"type": "string"}}})


class CopilotTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.redactor = Redactor()
        self.http = HttpClient(timeout=5, redactor=self.redactor)

    def tearDown(self):
        self.server.close()

    def test_token_helpers(self):
        self.assertTrue(is_source_token("ghp_abc"))
        self.assertTrue(is_source_token("github_pat_abc"))
        self.assertFalse(is_source_token("tid=123;exp=456"))
        self.assertEqual(api_base_from_token("tid=1;proxy-ep=proxy.individual.githubcopilot.com;exp=2"), "https://api.individual.githubcopilot.com")
        self.assertIsNone(api_base_from_token("tid=1;exp=2"))

    def test_exchange_then_responses_call(self):
        session_token = "tid=abc;exp=9999;proxy-ep=proxy.individual.githubcopilot.com"
        self.server.routes[("GET", "/copilot_internal/v2/token")] = lambda r: (200, {"token": session_token, "expires_at": int(time.time()) + 3600, "endpoints": {"api": self.server.url}})
        self.server.routes[("POST", "/responses")] = lambda r: (200, _responses_reply("hello"))
        provider = CopilotProvider(token="ghp_sourceTokenValue1234567890", model="gpt-6-sol", effort="medium", http=self.http, redactor=self.redactor, github_api_base_url=self.server.url)
        reply = provider.complete(system="sys", messages=[Message(role="user", content="hi")], tools=[TOOL])
        self.assertEqual(reply.text, "hello")
        self.assertEqual(provider.api_base_url, self.server.url)
        exchange, call = self.server.requests
        self.assertEqual(exchange["headers"]["authorization"], "Bearer ghp_sourceTokenValue1234567890")
        self.assertEqual(exchange["headers"]["copilot-integration-id"], "vscode-chat")
        self.assertEqual(call["headers"]["authorization"], f"Bearer {session_token}")
        self.assertEqual(call["headers"]["openai-intent"], "conversation-edits")
        self.assertEqual(call["headers"]["x-initiator"], "agent")
        body = call["body"]
        self.assertEqual(body["model"], "gpt-6-sol")
        self.assertEqual(body["instructions"], "sys")
        self.assertEqual(body["reasoning"], {"effort": "medium"})
        self.assertFalse(body["stream"])
        self.assertEqual(body["input"][0]["content"][0]["type"], "input_text")
        self.assertEqual(body["tools"][0]["name"], "bash")
        # Secrets picked up along the way are masked.
        self.assertEqual(self.redactor.redact(session_token), "***")

    def test_session_token_used_directly(self):
        self.server.routes[("POST", "/responses")] = lambda r: (200, _responses_reply())
        provider = CopilotProvider(token="tid=session;exp=1", model="gpt-6-sol", http=self.http, api_base_url=self.server.url)
        provider.complete(system="", messages=[Message(role="user", content="hi")], tools=[])
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(provider.token_source, "copilot_token")
        self.assertNotIn("instructions", self.server.requests[0]["body"])

    def test_expired_token_is_refreshed_once(self):
        counter = {"exchanges": 0, "calls": 0}

        def exchange(record):
            counter["exchanges"] += 1
            return 200, {"token": f"tid=t{counter['exchanges']};exp=1", "expires_at": int(time.time()) + 3600}

        def responses(record):
            counter["calls"] += 1
            if counter["calls"] == 1:
                return 401, {"error": {"message": "token expired"}}
            return 200, _responses_reply("after refresh")

        self.server.routes[("GET", "/copilot_internal/v2/token")] = exchange
        self.server.routes[("POST", "/responses")] = responses
        provider = CopilotProvider(token="gho_sourceTokenValue1234567890", model="gpt-6-sol", http=self.http, github_api_base_url=self.server.url, api_base_url=self.server.url)
        reply = provider.complete(system="", messages=[Message(role="user", content="hi")], tools=[])
        self.assertEqual(reply.text, "after refresh")
        self.assertEqual(counter["exchanges"], 2)

    def test_exchange_failure_is_authentication_error(self):
        self.server.routes[("GET", "/copilot_internal/v2/token")] = lambda r: (401, {"message": "Bad credentials"})
        provider = CopilotProvider(token="ghp_sourceTokenValue1234567890", model="gpt-6-sol", http=self.http, github_api_base_url=self.server.url)
        with self.assertRaises(AuthenticationError):
            provider.complete(system="", messages=[Message(role="user", content="hi")], tools=[])

    def test_overflow_and_transient_mapping(self):
        self.server.routes[("POST", "/responses")] = lambda r: (400, {"error": {"message": "Your input exceeds the context window of this model"}})
        provider = CopilotProvider(token="tid=s;exp=1", model="gpt-6-sol", http=self.http, api_base_url=self.server.url)
        with self.assertRaises(ContextOverflowError):
            provider.complete(system="", messages=[Message(role="user", content="hi")], tools=[])
        self.server.routes[("POST", "/responses")] = lambda r: (503, {"error": {"message": "busy"}})
        with self.assertRaises(TransientProviderError):
            provider.complete(system="", messages=[Message(role="user", content="hi")], tools=[])

    def test_noop_tool_added_when_history_has_calls_but_no_tools(self):
        self.server.routes[("POST", "/responses")] = lambda r: (200, _responses_reply())
        provider = CopilotProvider(token="tid=s;exp=1", model="gpt-6-sol", http=self.http, api_base_url=self.server.url)
        from pipeline_agent.session import ToolCall

        messages = [
            Message(role="user", content="x"),
            Message(role="assistant", content="", tool_calls=[ToolCall(id="c1", name="bash", arguments={"command": "ls"})]),
            Message(role="tool", content="out", tool_call_id="c1", name="bash"),
        ]
        provider.complete(system="", messages=messages, tools=[])
        self.assertEqual(self.server.requests[0]["body"]["tools"][0]["name"], "_noop")


class AIPlatformTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.redactor = Redactor()
        self.http = HttpClient(timeout=5, redactor=self.redactor)
        self.env = {
            "AI_PLATFORM_CHAT_HOST": self.server.url,
            "AI_PLATFORM_IB2B_HOST": self.server.url,
            "AI_PLATFORM_USERNAME": "svc-user",
            "AI_PLATFORM_PASSWORD": "svc-password-123",
            "AI_PLATFORM_USERCASE": "usecase-42",
            "AI_PLATFORM_TRUST_TOKEN_HEADER": "X-Corp-E2E-Trust-Token",
            "AI_PLATFORM_TRACKING_PREFIX": "TEST",
        }
        self.tokens = {"issued": 0}

        def sts(record):
            self.tokens["issued"] += 1
            body = record["body"]
            assert body["input_token_state"]["username"] == "svc-user"
            assert body["output_token_state"]["token_type"] == "JWT"
            return 200, {"issued_token": f"jwt-token-{self.tokens['issued']}"}

        self.server.routes[("POST", "/dsp/token")] = sts

    def tearDown(self):
        self.server.close()

    def test_settings_validation(self):
        with self.assertRaises(AuthenticationError):
            AIPlatformSettings.from_env({}).validate()
        settings = AIPlatformSettings.from_env(self.env)
        settings.validate()
        self.assertEqual(settings.endpoint_url, self.server.url + "/v1/api/v1/chat/completions")
        self.assertEqual(settings.ib2b_url, self.server.url + "/dsp/token")
        self.assertTrue(is_responses_endpoint("https://h/v1/x/responses"))
        self.assertTrue(is_responses_endpoint("https://h/v1/x/responses/compact"))
        self.assertFalse(is_responses_endpoint("https://h/v1/api/v1/chat/completions"))

    def test_chat_completions_flow(self):
        self.server.routes[("POST", "/v1/api/v1/chat/completions")] = lambda r: (200, _chat_reply("chat ok"))
        provider = AIPlatformProvider(AIPlatformSettings.from_env(self.env), model="gpt-5.4", effort="high", http=self.http, redactor=self.redactor)
        reply = provider.complete(system="sys", messages=[Message(role="user", content="hi")], tools=[TOOL])
        self.assertEqual(reply.text, "chat ok")
        sts, call = self.server.requests
        self.assertEqual(call["headers"]["x-corp-e2e-trust-token"], "jwt-token-1")
        self.assertTrue(call["headers"]["x-correlation-id"].startswith("TEST-"))
        self.assertEqual(call["headers"]["x-correlation-id"], call["headers"]["x-usersession-id"])
        self.assertNotIn("authorization", call["headers"])
        body = call["body"]
        self.assertEqual(body["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(body["tools"][0]["function"]["name"], "bash")
        self.assertEqual(body["reasoning_effort"], "high")
        self.assertEqual(body["user"], "usecase-42")
        self.assertEqual(self.redactor.redact("jwt-token-1 svc-password-123"), "*** ***")

    def test_responses_endpoint_flow_and_reexchange(self):
        env = dict(self.env)
        env["AI_PLATFORM_RESPONSES_URI"] = "/v1/usecase-42/responses"
        calls = {"n": 0}

        def responses(record):
            calls["n"] += 1
            if calls["n"] == 1:
                return 403, {"error": {"message": "forbidden"}}
            return 200, _responses_reply("responses ok")

        self.server.routes[("POST", "/v1/usecase-42/responses")] = responses
        provider = AIPlatformProvider(AIPlatformSettings.from_env(env), model="gpt-5.6-sol", effort="low", http=self.http, redactor=self.redactor)
        self.assertTrue(provider.uses_responses)
        reply = provider.complete(system="sys", messages=[Message(role="user", content="hi")], tools=[TOOL])
        self.assertEqual(reply.text, "responses ok")
        self.assertEqual(self.tokens["issued"], 2)
        last = self.server.requests[-1]
        self.assertEqual(last["headers"]["authorization"], "Bearer jwt-token-2")
        self.assertEqual(last["headers"]["x-corp-e2e-trust-token"], "jwt-token-2")
        body = last["body"]
        self.assertEqual(body["instructions"], "sys")
        self.assertEqual(body["reasoning"], {"effort": "low"})
        self.assertEqual(body["input"][0]["role"], "user")
        self.assertNotIn("user", body)

    def test_static_token_without_exchange(self):
        env = {k: v for k, v in self.env.items() if not k.endswith(("USERNAME", "PASSWORD", "IB2B_HOST"))}
        env["AI_PLATFORM_TOKEN"] = "static-jwt-token"
        self.server.routes[("POST", "/v1/api/v1/chat/completions")] = lambda r: (200, _chat_reply())
        provider = AIPlatformProvider(AIPlatformSettings.from_env(env), model="gpt-5.4", http=self.http, redactor=self.redactor)
        provider.complete(system="", messages=[Message(role="user", content="hi")], tools=[])
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(self.server.requests[0]["headers"]["x-corp-e2e-trust-token"], "static-jwt-token")


if __name__ == "__main__":
    unittest.main()
