"""The ask_user tool: park the run until a person answers in Jenkins."""

from __future__ import annotations

from typing import Any

from .base import ToolContext, ToolError, ToolResult, ToolSpec

OTHER_LABEL = "[other: see text answer]"

SPEC = ToolSpec(
    name="ask_user",
    description=(
        "Ask the person who started the build to decide something, and pause until they answer. "
        "Use it only when the next step depends on a choice that is genuinely theirs: which of several "
        "viable approaches to take, which branch or version to target, or how to read an ambiguous request. "
        "Never ask for something you can find out yourself. Put every open question into one call, give each "
        "a short header, and offer concrete options with a one-line description where possible; a free-text "
        "answer is always possible unless allow_custom is false. The answer arrives as the tool result."
    ),
    parameters={
        "type": "object",
        "properties": {
            "questions": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "The full question."},
                        "header": {"type": "string", "description": "Two to four words naming the decision."},
                        "options": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "description": {"type": "string"},
                                },
                                "required": ["label"],
                                "additionalProperties": False,
                            },
                        },
                        "allow_custom": {"type": "boolean", "description": "Whether a free-text answer is accepted. Default true."},
                    },
                    "required": ["question"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["questions"],
        "additionalProperties": False,
    },
)


def normalize_questions(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ToolError("'questions' must be a non-empty array")
    questions: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ToolError("each question must be an object")
        text = str(item.get("question", "")).strip()
        if not text:
            raise ToolError("each question needs a non-empty 'question'")
        options: list[dict[str, str]] = []
        for option in item.get("options") or []:
            if isinstance(option, str):
                label, description = option, ""
            elif isinstance(option, dict):
                label = str(option.get("label", "")).strip()
                description = str(option.get("description", "") or "").strip()
            else:
                continue
            if label:
                options.append({"label": label, "description": description})
        allow_custom = item.get("allow_custom")
        questions.append(
            {
                "question": text,
                "header": str(item.get("header", "") or "").strip()[:40],
                "options": options,
                "allow_custom": True if allow_custom is None else bool(allow_custom),
            }
        )
    return questions


def format_answers(questions: list[dict[str, Any]], answers: dict[int, str]) -> str:
    lines = ["The user answered:"]
    for index, question in enumerate(questions):
        answer = answers.get(index, "").strip()
        lines.append(f"{index + 1}. {question['question']}")
        lines.append(f"   Answer: {answer if answer else '(no answer given)'}")
    lines.append("Continue with these answers in mind.")
    return "\n".join(lines)


class AskUserTool:
    spec = SPEC

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        questions = normalize_questions(args.get("questions"))
        return ToolResult(
            content="Waiting for the user's answer.",
            success=True,
            meta={"pause": "question", "questions": questions},
        )


__all__ = ["AskUserTool", "OTHER_LABEL", "SPEC", "format_answers", "normalize_questions"]
