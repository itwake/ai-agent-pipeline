"""Conversation model and the on-disk session store.

A session is the agent's memory for one Jenkins build. It is rewritten on every
change so a later process (the next pipeline step) can resume exactly where the
previous one stopped. A separate append-only transcript keeps everything that
ever happened, including messages that compaction later replaced.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: Optional[dict[str, Any]]
    arguments_text: str = ""

    @property
    def parsed(self) -> bool:
        return self.arguments is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
            "arguments_text": self.arguments_text,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolCall":
        return cls(
            id=str(data.get("id", "")),
            name=str(data.get("name", "")),
            arguments=data.get("arguments"),
            arguments_text=str(data.get("arguments_text", "") or ""),
        )


@dataclass
class Message:
    """One turn in the conversation.

    role is one of ``user``, ``assistant`` or ``tool``. Tool messages carry the
    id of the call they answer. Assistant messages may carry tool calls.
    """

    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: Optional[str] = None
    name: Optional[str] = None
    meta: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "role": self.role,
            "content": self.content,
            "created_at": self.created_at,
        }
        if self.tool_calls:
            data["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        if self.tool_call_id is not None:
            data["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            data["name"] = self.name
        if self.meta:
            data["meta"] = self.meta
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Message":
        return cls(
            role=str(data.get("role", "user")),
            content=str(data.get("content", "") or ""),
            tool_calls=[ToolCall.from_dict(item) for item in data.get("tool_calls", []) or []],
            tool_call_id=data.get("tool_call_id"),
            name=data.get("name"),
            meta=dict(data.get("meta", {}) or {}),
            created_at=str(data.get("created_at") or utc_now()),
        )


@dataclass
class PendingQuestion:
    """A question the model asked; the run is parked until it is answered."""

    tool_call_id: str
    questions: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {"tool_call_id": self.tool_call_id, "questions": self.questions}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingQuestion":
        return cls(
            tool_call_id=str(data.get("tool_call_id", "")),
            questions=list(data.get("questions", []) or []),
        )


@dataclass
class Session:
    session_id: str
    messages: list[Message] = field(default_factory=list)
    status: str = "new"
    iterations: int = 0
    usage: dict[str, int] = field(default_factory=lambda: {"input_tokens": 0, "output_tokens": 0, "model_calls": 0})
    pending_question: Optional[PendingQuestion] = None
    info: dict[str, Any] = field(default_factory=dict)
    compactions: int = 0
    last_error: Optional[str] = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def add_usage(self, usage: dict[str, Any]) -> None:
        for key in ("input_tokens", "output_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                self.usage[key] = int(self.usage.get(key, 0)) + int(value)
        self.usage["model_calls"] = int(self.usage.get("model_calls", 0)) + 1

    def last_assistant_text(self) -> str:
        for message in reversed(self.messages):
            if message.role == "assistant" and message.content:
                return message.content
        return ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "iterations": self.iterations,
            "usage": dict(self.usage),
            "pending_question": self.pending_question.to_dict() if self.pending_question else None,
            "info": self.info,
            "compactions": self.compactions,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "messages": [message.to_dict() for message in self.messages],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        pending = data.get("pending_question")
        return cls(
            session_id=str(data.get("session_id", "")),
            messages=[Message.from_dict(item) for item in data.get("messages", []) or []],
            status=str(data.get("status", "new")),
            iterations=int(data.get("iterations", 0) or 0),
            usage={k: int(v) for k, v in (data.get("usage") or {}).items()} or {"input_tokens": 0, "output_tokens": 0, "model_calls": 0},
            pending_question=PendingQuestion.from_dict(pending) if isinstance(pending, dict) else None,
            info=dict(data.get("info", {}) or {}),
            compactions=int(data.get("compactions", 0) or 0),
            last_error=data.get("last_error"),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
        )


_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")


def safe_session_id(session_id: str) -> str:
    cleaned = _SAFE_ID.sub("-", session_id.strip()).strip("-.")
    return cleaned[:120] or "session"


class SessionStore:
    """JSON file per session plus an append-only transcript."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id: str) -> Path:
        return self.root / f"{safe_session_id(session_id)}.json"

    def transcript_path(self, session_id: str) -> Path:
        return self.root / f"{safe_session_id(session_id)}.transcript.jsonl"

    def exists(self, session_id: str) -> bool:
        return self.path(session_id).exists()

    def load(self, session_id: str) -> Session:
        path = self.path(session_id)
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return Session.from_dict(data)

    def save(self, session: Session) -> Path:
        session.updated_at = utc_now()
        path = self.path(session.session_id)
        payload = json.dumps(session.to_dict(), ensure_ascii=False, indent=2)
        fd, tmp_name = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(self.root))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return path

    def append_transcript(self, session_id: str, record: dict[str, Any]) -> None:
        entry = dict(record)
        entry.setdefault("at", utc_now())
        with self.transcript_path(session_id).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


__all__ = [
    "Message",
    "PendingQuestion",
    "Session",
    "SessionStore",
    "ToolCall",
    "safe_session_id",
    "utc_now",
]
