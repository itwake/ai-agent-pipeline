"""Keep the conversation inside the model's context window.

Three measures, applied in order of how much information they lose:

1. prune: old tool outputs are cut down to a short head plus a pointer to the
   archived full text;
2. summarize: everything between the task prompt and the recent tail is
   replaced by a model-written summary;
3. hard prune: as a last resort every tool output, recent ones included, is
   cut down.

Token counts are estimated at four characters per token; the budget already
holds back a safety margin so the estimate can be off by a fair amount.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

from .providers.base import Provider, ProviderError
from .session import Message, Session, SessionStore
from .tools.base import ToolSpec

CHARS_PER_TOKEN = 4
SUMMARY_MARKER = "[Context summary: earlier turns were compacted to save context]"

SUMMARY_SYSTEM_PROMPT = """You are compacting the working memory of an autonomous software engineering agent that is in the middle of a task inside a CI build. Write a dense, factual summary of the transcript you are given so the agent can continue without the original messages.

Include, under these headings:
## Task
The original request, verbatim where short, plus any constraints the user gave.
## Progress
What has been done so far, in order, and what is in progress right now.
## Key facts
Repository, branch names, file paths, dependency coordinates and versions, build commands that work, configuration details, exact error messages that still matter.
## Changes made
Files changed and how, commits created (hash and message), whether anything was pushed.
## Failures and dead ends
Commands or approaches that failed and why, so they are not repeated.
## User decisions
Questions asked and the answers received.
## Next steps
What remains to be done.

Preserve exact identifiers (paths, versions, hashes, command lines). Do not invent anything. Plain Markdown, no preamble."""


@dataclass
class ContextSettings:
    budget_tokens: int
    keep_recent: int = 12
    prune_min_chars: int = 3_000
    prune_keep_chars: int = 1_200
    hard_prune_keep_chars: int = 600
    summary_input_max_chars: int = 600_000


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN + 1


def estimate_request_tokens(system: str, messages: list[Message], tools: list[ToolSpec]) -> int:
    total = estimate_tokens(system)
    for message in messages:
        total += estimate_tokens(message.content) + 4
        for call in message.tool_calls:
            total += estimate_tokens(call.arguments_text or json.dumps(call.arguments or {})) + 8
    for spec in tools:
        total += estimate_tokens(spec.description) + estimate_tokens(json.dumps(spec.parameters)) + 8
    return total


class ContextManager:
    def __init__(
        self,
        provider: Provider,
        settings: ContextSettings,
        *,
        store: Optional[SessionStore] = None,
        console: Any = None,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self.store = store
        self.console = console

    # --- public ---------------------------------------------------------

    def prepare(self, session: Session, system: str, tools: list[ToolSpec]) -> int:
        """Shrink the session in place until the request fits. Returns the estimate."""

        estimate = estimate_request_tokens(system, session.messages, tools)
        if estimate <= self.settings.budget_tokens:
            return estimate
        self._log(f"context estimate {estimate:,} tokens exceeds budget {self.settings.budget_tokens:,}; pruning old tool output")
        self._prune(session, keep_recent=self.settings.keep_recent, keep_chars=self.settings.prune_keep_chars)
        estimate = estimate_request_tokens(system, session.messages, tools)
        if estimate <= self.settings.budget_tokens:
            return estimate
        self.compact(session, reason="budget")
        estimate = estimate_request_tokens(system, session.messages, tools)
        if estimate <= self.settings.budget_tokens:
            return estimate
        self._log("still over budget after summarizing; cutting all tool output down")
        self._prune(session, keep_recent=0, keep_chars=self.settings.hard_prune_keep_chars, min_chars=self.settings.hard_prune_keep_chars * 2)
        return estimate_request_tokens(system, session.messages, tools)

    def compact(self, session: Session, *, reason: str) -> bool:
        """Replace the middle of the conversation with a summary. True when something changed."""

        messages = session.messages
        cut = self._cut_index(messages)
        if cut is None:
            self._log("nothing to summarize yet; pruning instead")
            self._prune(session, keep_recent=0, keep_chars=self.settings.hard_prune_keep_chars, min_chars=self.settings.hard_prune_keep_chars * 2)
            return False
        head, tail = messages[1:cut], messages[cut:]
        transcript = self._render_transcript(head)
        self._log(f"summarizing {len(head)} messages ({len(transcript):,} chars), keeping the last {len(tail)}; reason: {reason}")
        try:
            reply = self.provider.complete(
                system=SUMMARY_SYSTEM_PROMPT,
                messages=[Message(role="user", content=transcript)],
                tools=[],
            )
            summary = reply.text.strip()
            session.add_usage(reply.usage)
        except ProviderError as exc:
            self._log(f"summary request failed ({exc}); falling back to pruning")
            self._prune(session, keep_recent=0, keep_chars=self.settings.hard_prune_keep_chars, min_chars=self.settings.hard_prune_keep_chars * 2)
            return False
        if not summary:
            self._log("summary came back empty; falling back to pruning")
            self._prune(session, keep_recent=0, keep_chars=self.settings.hard_prune_keep_chars, min_chars=self.settings.hard_prune_keep_chars * 2)
            return False
        summary_message = Message(
            role="user",
            content=f"{SUMMARY_MARKER}\n\n{summary}",
            meta={"kind": "summary", "replaced_messages": len(head)},
        )
        session.messages = [messages[0], summary_message, *tail]
        session.compactions += 1
        if self.store is not None:
            self.store.append_transcript(
                session.session_id,
                {"event": "compaction", "reason": reason, "replaced": len(head), "summary": summary},
            )
        return True

    # --- internals -------------------------------------------------------

    def _cut_index(self, messages: list[Message]) -> Optional[int]:
        keep = max(0, self.settings.keep_recent)
        cut = len(messages) - keep
        if cut < 3:
            return None
        while cut < len(messages) and messages[cut].role == "tool":
            cut += 1
        if cut < 3 or cut >= len(messages):
            return None
        return cut

    def _prune(self, session: Session, *, keep_recent: int, keep_chars: int, min_chars: Optional[int] = None) -> int:
        threshold = self.settings.prune_min_chars if min_chars is None else min_chars
        limit = len(session.messages) - max(0, keep_recent)
        saved = 0
        for message in session.messages[:limit]:
            if message.role != "tool" or len(message.content) <= threshold:
                continue
            original = int(message.meta.get("original_chars") or len(message.content))
            archive = message.meta.get("archive")
            pointer = f" The full output was saved to {archive}." if archive else ""
            head = message.content[:keep_chars].rstrip()
            message.content = f"{head}\n[earlier tool output cut to save context: {original} characters in total.{pointer}]"
            message.meta["pruned"] = True
            message.meta["original_chars"] = original
            saved += original - len(message.content)
        return saved

    def _render_transcript(self, messages: list[Message]) -> str:
        chunks: list[str] = []
        for message in messages:
            if message.role == "tool":
                content = message.content
                if len(content) > 2_000:
                    content = content[:2_000] + "\n[... output shortened ...]"
                chunks.append(f"[tool result: {message.name or message.tool_call_id}]\n{content}")
            elif message.role == "assistant":
                if message.content:
                    chunks.append(f"[assistant]\n{message.content}")
                for call in message.tool_calls:
                    args = call.arguments_text or json.dumps(call.arguments or {})
                    if len(args) > 800:
                        args = args[:800] + " ..."
                    chunks.append(f"[tool call: {call.name}] {args}")
            else:
                chunks.append(f"[{message.role}]\n{message.content}")
        transcript = "\n\n".join(chunks)
        limit = self.settings.summary_input_max_chars
        if len(transcript) > limit:
            transcript = "[earliest part of the transcript omitted]\n\n" + transcript[-limit:]
        return transcript

    def _log(self, text: str) -> None:
        if self.console is not None:
            self.console.info(f"context: {text}")


__all__ = ["CHARS_PER_TOKEN", "ContextManager", "ContextSettings", "SUMMARY_MARKER", "estimate_request_tokens", "estimate_tokens"]
