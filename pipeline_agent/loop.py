"""The agent loop: model call, tool execution, pause and resume."""

from __future__ import annotations

import json
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .console import Console
from .context import ContextManager
from .providers.base import ContextOverflowError, ModelReply, Provider, ProviderError, TransientProviderError
from .session import Message, PendingQuestion, Session, SessionStore, ToolCall
from .tools import ToolContext, ToolError, ToolRegistry, ToolResult
from .tools.ask_user import format_answers

STATUS_COMPLETED = "completed"
STATUS_WAITING_FOR_QUESTION = "waiting_for_question"
STATUS_MAX_ITERATIONS = "max_iterations"
STATUS_ERROR = "error"
STATUS_RUNNING = "running"


@dataclass
class LoopSettings:
    max_iterations: int = 200
    doom_loop_threshold: int = 3
    provider_retries: int = 3
    retry_backoff_seconds: float = 3.0


@dataclass
class RunOutcome:
    status: str
    session: Session
    final_text: str = ""
    questions: list[dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


class AgentLoop:
    def __init__(
        self,
        *,
        provider: Provider,
        registry: ToolRegistry,
        store: SessionStore,
        context: ContextManager,
        system_prompt: str,
        tool_context_factory: Callable[[str, str], ToolContext],
        console: Console,
        settings: Optional[LoopSettings] = None,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.store = store
        self.context = context
        self.system_prompt = system_prompt
        self.tool_context_factory = tool_context_factory
        self.console = console
        self.settings = settings or LoopSettings()

    # --- entry points ----------------------------------------------------

    def start(self, session_id: str, prompt: str, info: Optional[dict[str, Any]] = None) -> RunOutcome:
        session = Session(session_id=session_id, info=dict(info or {}))
        self._append(session, Message(role="user", content=prompt))
        self.store.append_transcript(session_id, {"event": "system_prompt", "text": self.system_prompt})
        return self._drive(session)

    def resume(self, session_id: str, *, answers: Optional[dict[int, str]] = None, more_iterations: int = 0) -> RunOutcome:
        session = self.store.load(session_id)
        if more_iterations > 0:
            session.info["extra_iterations"] = int(session.info.get("extra_iterations", 0)) + int(more_iterations)
        if session.pending_question is not None:
            if answers is None:
                outcome = RunOutcome(status=STATUS_WAITING_FOR_QUESTION, session=session, questions=session.pending_question.questions)
                self.console.warn("the session is waiting for an answer; resume with --answers-file")
                return outcome
            self._apply_answers(session, answers)
        self.store.append_transcript(session_id, {"event": "resume", "answers": answers, "more_iterations": more_iterations})
        return self._drive(session)

    # --- core ------------------------------------------------------------

    def _iteration_limit(self, session: Session) -> int:
        return self.settings.max_iterations + int(session.info.get("extra_iterations", 0))

    def _drive(self, session: Session) -> RunOutcome:
        session.status = STATUS_RUNNING
        session.last_error = None
        tools = self.registry.specs()
        try:
            while True:
                pause = self._settle_tool_calls(session)
                if pause is not None:
                    return pause
                limit = self._iteration_limit(session)
                if session.iterations >= limit:
                    session.status = STATUS_MAX_ITERATIONS
                    self.store.save(session)
                    self.console.warn(f"iteration limit of {limit} reached; the build can grant more and resume")
                    return RunOutcome(status=STATUS_MAX_ITERATIONS, session=session, final_text=session.last_assistant_text())
                estimate = self.context.prepare(session, self.system_prompt, tools)
                self.console.model_call(session.iterations + 1, limit, estimate, self.provider.model)
                reply = self._call_model(session, tools)
                session.iterations += 1
                session.add_usage(reply.usage)
                message = Message(role="assistant", content=reply.text, tool_calls=reply.tool_calls, meta={"finish_reason": reply.finish_reason})
                self._append(session, message)
                self.console.assistant(reply.text)
                if not reply.tool_calls:
                    session.status = STATUS_COMPLETED
                    self.store.save(session)
                    return RunOutcome(status=STATUS_COMPLETED, session=session, final_text=reply.text)
                self.store.save(session)
        except ProviderError as exc:
            session.status = STATUS_ERROR
            session.last_error = str(exc)
            self.store.save(session)
            self.console.error(f"model request failed: {exc}")
            return RunOutcome(status=STATUS_ERROR, session=session, final_text=session.last_assistant_text(), error=str(exc))
        except KeyboardInterrupt:
            session.status = STATUS_ERROR
            session.last_error = "interrupted"
            self.store.save(session)
            raise

    def _call_model(self, session: Session, tools: list) -> ModelReply:
        attempts = 0
        overflow_retried = False
        while True:
            try:
                return self.provider.complete(system=self.system_prompt, messages=session.messages, tools=tools)
            except ContextOverflowError as exc:
                if overflow_retried:
                    raise
                overflow_retried = True
                self.console.warn(f"context overflow reported by the model service; compacting and retrying ({exc})")
                self.context.compact(session, reason="overflow")
                self.store.save(session)
            except TransientProviderError as exc:
                attempts += 1
                if attempts > self.settings.provider_retries:
                    raise
                delay = self.settings.retry_backoff_seconds * (2 ** (attempts - 1))
                self.console.warn(f"transient model error ({exc}); retry {attempts}/{self.settings.provider_retries} in {delay:.0f}s")
                time.sleep(delay)

    # --- tool execution --------------------------------------------------

    def _settle_tool_calls(self, session: Session) -> Optional[RunOutcome]:
        """Run every tool call that has no result yet. Returns an outcome when the run must pause."""

        assistant_index = self._last_assistant_index(session)
        if assistant_index is None:
            return None
        assistant = session.messages[assistant_index]
        answered = {m.tool_call_id for m in session.messages[assistant_index + 1 :] if m.role == "tool"}
        for call in assistant.tool_calls:
            if call.id in answered:
                continue
            if session.pending_question is not None and session.pending_question.tool_call_id == call.id:
                # Still unanswered: the pipeline has to ask the user first.
                self.store.save(session)
                return RunOutcome(status=STATUS_WAITING_FOR_QUESTION, session=session, questions=session.pending_question.questions)
            result = self._execute(session, call)
            if result.pause == "question":
                questions = list(result.meta.get("questions") or [])
                session.pending_question = PendingQuestion(tool_call_id=call.id, questions=questions)
                session.status = STATUS_WAITING_FOR_QUESTION
                self.store.save(session)
                self.store.append_transcript(session.session_id, {"event": "question", "tool_call_id": call.id, "questions": questions})
                self.console.question(questions)
                return RunOutcome(status=STATUS_WAITING_FOR_QUESTION, session=session, questions=questions)
            self._append(session, Message(role="tool", content=result.content, tool_call_id=call.id, name=call.name, meta=_result_meta(result)))
            self.store.save(session)
        return None

    def _execute(self, session: Session, call: ToolCall) -> ToolResult:
        tool = self.registry.get(call.name)
        self.console.tool_call(call.name, call.arguments if call.arguments is not None else call.arguments_text)
        if tool is None:
            result = ToolResult(content=f"Unknown tool '{call.name}'. Available tools: {', '.join(self.registry.names())}.", success=False)
        elif call.arguments is None:
            result = ToolResult(content="The tool arguments were not a valid JSON object. Send the call again with well-formed JSON.", success=False)
        elif self._is_doom_loop(session, call):
            result = ToolResult(
                content=(
                    f"This exact {call.name} call has already been made {self.settings.doom_loop_threshold} times in a row "
                    "with the same arguments. It was not run again. Change the approach, or ask the user if you are stuck."
                ),
                success=False,
                meta={"blocked": True, "reason": "repeated call"},
            )
        else:
            ctx = self.tool_context_factory(session.session_id, call.id)
            try:
                result = tool.run(call.arguments, ctx)
            except ToolError as exc:
                result = ToolResult(content=f"Error: {exc}", success=False)
            except Exception as exc:  # noqa: BLE001 - the model needs to see the failure, not the process
                self.console.error(f"tool {call.name} crashed: {exc}\n{traceback.format_exc()}")
                result = ToolResult(content=f"The tool failed unexpectedly: {type(exc).__name__}: {exc}", success=False)
        self.console.tool_result(call.name, result)
        return result

    def _is_doom_loop(self, session: Session, call: ToolCall) -> bool:
        threshold = self.settings.doom_loop_threshold
        if threshold < 2:
            return False
        signature = (call.name, _stable_arguments(call))
        recent: list[tuple[str, str]] = []
        for message in reversed(session.messages):
            if message.role != "assistant":
                continue
            for previous in reversed(message.tool_calls):
                recent.append((previous.name, _stable_arguments(previous)))
            if len(recent) > threshold:
                break
        # The current call is already part of the history (it sits in the last assistant message).
        executed = recent[1 : threshold + 1] if recent and recent[0] == signature else recent[:threshold]
        if len(executed) < threshold:
            return False
        return all(item == signature for item in executed)

    def _apply_answers(self, session: Session, answers: dict[int, str]) -> None:
        pending = session.pending_question
        if pending is None:
            return
        content = format_answers(pending.questions, answers)
        self._append(session, Message(role="tool", content=content, tool_call_id=pending.tool_call_id, name="ask_user", meta={"answers": answers}))
        session.pending_question = None
        session.status = STATUS_RUNNING
        self.console.info("user answers received:\n" + content)

    # --- helpers ---------------------------------------------------------

    @staticmethod
    def _last_assistant_index(session: Session) -> Optional[int]:
        for index in range(len(session.messages) - 1, -1, -1):
            if session.messages[index].role == "assistant":
                return index if session.messages[index].tool_calls else None
            if session.messages[index].role == "user":
                return None
        return None

    def _append(self, session: Session, message: Message) -> None:
        session.messages.append(message)
        self.store.append_transcript(session.session_id, {"event": "message", **message.to_dict()})


def _result_meta(result: ToolResult) -> dict[str, Any]:
    meta: dict[str, Any] = {"success": result.success}
    for key in ("exit_code", "timed_out", "archive", "truncated", "duration_seconds", "blocked"):
        if key in result.meta:
            meta[key] = result.meta[key]
    return meta


def _stable_arguments(call: ToolCall) -> str:
    if call.arguments is not None:
        return json.dumps(call.arguments, sort_keys=True, ensure_ascii=False)
    return call.arguments_text


__all__ = [
    "AgentLoop",
    "LoopSettings",
    "RunOutcome",
    "STATUS_COMPLETED",
    "STATUS_ERROR",
    "STATUS_MAX_ITERATIONS",
    "STATUS_RUNNING",
    "STATUS_WAITING_FOR_QUESTION",
]
