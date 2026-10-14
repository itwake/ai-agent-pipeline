"""State files the pipeline reads between agent invocations.

``state.properties`` is a flat ``KEY=value`` file so a Jenkinsfile can parse
it with nothing more than readFile and String methods. Values are kept on one
line: newlines are escaped as ``\\n`` and backslashes as ``\\\\``.
``state.json`` carries the same data for people and scripts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .loop import (
    STATUS_COMPLETED,
    STATUS_ERROR,
    STATUS_MAX_ITERATIONS,
    STATUS_WAITING_FOR_QUESTION,
    RunOutcome,
)

EXIT_CODES = {
    STATUS_COMPLETED: 0,
    STATUS_WAITING_FOR_QUESTION: 10,
    STATUS_MAX_ITERATIONS: 12,
    STATUS_ERROR: 1,
}
EXIT_USAGE = 2
STATE_JSON = "state.json"
STATE_PROPERTIES = "state.properties"
FINAL_MESSAGE = "final_message.md"
QUESTIONS_JSON = "questions.json"


def escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("\\", "\\\\").replace("\r", "").replace("\n", "\\n")


def unescape(value: str) -> str:
    out: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            nxt = value[index + 1]
            if nxt == "n":
                out.append("\n")
                index += 2
                continue
            if nxt == "\\":
                out.append("\\")
                index += 2
                continue
        out.append(char)
        index += 1
    return "".join(out)


def exit_code_for(status: str) -> int:
    return EXIT_CODES.get(status, 1)


def build_state(outcome: RunOutcome) -> dict[str, Any]:
    session = outcome.session
    return {
        "status": outcome.status,
        "exit_code": exit_code_for(outcome.status),
        "session_id": session.session_id,
        "iterations": session.iterations,
        "usage": dict(session.usage),
        "compactions": session.compactions,
        "final_message": outcome.final_text or "",
        "error": outcome.error or session.last_error or "",
        "questions": list(outcome.questions or []),
        "pending_tool_call_id": session.pending_question.tool_call_id if session.pending_question else "",
        "info": dict(session.info),
    }


def write_state(state_dir: str | Path, outcome: RunOutcome) -> dict[str, Any]:
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    state = build_state(outcome)
    (directory / STATE_JSON).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / FINAL_MESSAGE).write_text(state["final_message"], encoding="utf-8")
    (directory / QUESTIONS_JSON).write_text(json.dumps(state["questions"], ensure_ascii=False, indent=2), encoding="utf-8")
    (directory / STATE_PROPERTIES).write_text(render_properties(state), encoding="utf-8")
    return state


def render_properties(state: dict[str, Any]) -> str:
    usage = state.get("usage") or {}
    lines = [
        f"STATUS={escape(state['status'])}",
        f"EXIT_CODE={state['exit_code']}",
        f"SESSION_ID={escape(state['session_id'])}",
        f"ITERATIONS={state['iterations']}",
        f"MODEL_CALLS={usage.get('model_calls', 0)}",
        f"INPUT_TOKENS={usage.get('input_tokens', 0)}",
        f"OUTPUT_TOKENS={usage.get('output_tokens', 0)}",
        f"COMPACTIONS={state.get('compactions', 0)}",
        f"ERROR={escape(state.get('error', ''))}",
        f"FINAL_MESSAGE={escape(state.get('final_message', ''))}",
    ]
    questions = state.get("questions") or []
    lines.append(f"QUESTION_COUNT={len(questions)}")
    for index, question in enumerate(questions):
        options = question.get("options") or []
        lines.append(f"Q{index}_HEADER={escape(question.get('header', ''))}")
        lines.append(f"Q{index}_TEXT={escape(question.get('question', ''))}")
        lines.append(f"Q{index}_CUSTOM={'true' if question.get('allow_custom', True) else 'false'}")
        lines.append(f"Q{index}_OPTION_COUNT={len(options)}")
        for option_index, option in enumerate(options):
            lines.append(f"Q{index}_OPT{option_index}_LABEL={escape(option.get('label', ''))}")
            lines.append(f"Q{index}_OPT{option_index}_DESC={escape(option.get('description', ''))}")
    return "\n".join(lines) + "\n"


def read_state(state_dir: str | Path) -> dict[str, Any]:
    path = Path(state_dir) / STATE_JSON
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def read_properties(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8")
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = unescape(value.strip())
    return values


def parse_answers_file(path: str | Path) -> dict[int, str]:
    """Answers come back as ``Q<index>=<text>`` lines (or ``<index>=<text>``)."""

    answers: dict[int, str] = {}
    for key, value in read_properties(path).items():
        name = key.upper()
        if name.startswith("Q"):
            name = name[1:]
        if name.endswith("_TEXT"):
            name = name[: -len("_TEXT")]
            # A free-text answer wins over the choice when both are present.
            if value.strip():
                if name.isdigit():
                    answers[int(name)] = value
            continue
        if name.isdigit() and int(name) not in answers:
            answers[int(name)] = value
    return answers


def state_field(state: dict[str, Any], field: str) -> Optional[str]:
    key = field.strip()
    properties = render_properties(state) if state else ""
    for line in properties.splitlines():
        if line.startswith(key + "="):
            return unescape(line[len(key) + 1 :])
    return None


__all__ = [
    "EXIT_CODES",
    "EXIT_USAGE",
    "FINAL_MESSAGE",
    "QUESTIONS_JSON",
    "STATE_JSON",
    "STATE_PROPERTIES",
    "build_state",
    "escape",
    "exit_code_for",
    "parse_answers_file",
    "read_properties",
    "read_state",
    "render_properties",
    "state_field",
    "unescape",
    "write_state",
]
