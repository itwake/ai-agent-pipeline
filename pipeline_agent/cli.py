"""Command line entry point used by the Jenkinsfile (and by people)."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from . import __version__
from .console import Console
from .context import ContextManager, ContextSettings
from .loop import STATUS_ERROR, AgentLoop, LoopSettings, RunOutcome
from .prompt import PromptContext, build_system_prompt
from .providers import ProviderSettings, build_provider, catalog, validate_model
from .providers.base import AuthenticationError, Provider, ProviderError
from .redaction import Redactor
from .session import Message, Session, SessionStore, safe_session_id
from .skills import Skill, discover_skills, find_skill
from .state import EXIT_USAGE, exit_code_for, parse_answers_file, read_state, render_properties, state_field, write_state
from .tools import CommandPolicy, ToolContext, ToolRegistry, default_registry

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SKILLS_DIR = PACKAGE_ROOT / "skills"
DEFAULT_DENY_FILE = PACKAGE_ROOT / "config" / "command-deny.txt"

DEFAULT_SECRET_ENV = (
    "COPILOT_GITHUB_TOKEN",
    "GITHUB_COPILOT_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "AI_PLATFORM_PASSWORD",
    "AI_PLATFORM_TOKEN",
    "GIT_PASSWORD",
)
DEFAULT_HIDDEN_ENV = (
    "COPILOT_GITHUB_TOKEN",
    "GITHUB_COPILOT_TOKEN",
    "AI_PLATFORM_USERNAME",
    "AI_PLATFORM_PASSWORD",
    "AI_PLATFORM_TOKEN",
)


class UsageError(Exception):
    pass


@dataclass
class Runtime:
    provider: Provider
    loop: AgentLoop
    store: SessionStore
    console: Console
    redactor: Redactor
    workspace: Path
    state_dir: Path
    skill: Optional[Skill]
    system_prompt: str
    model_window: int
    budget_tokens: int
    settings: ProviderSettings


# --- argument parsing ---------------------------------------------------------


def add_common_arguments(parser: argparse.ArgumentParser) -> None:
    env = os.environ
    parser.add_argument("--provider", default=env.get("AGENT_PROVIDER", catalog.GITHUB_COPILOT), help="github_copilot, ai_platform or fake")
    parser.add_argument("--model", default=env.get("AGENT_MODEL", ""), help="model id; defaults to the provider's default")
    parser.add_argument("--effort", "--thinking-level", dest="effort", default=env.get("AGENT_EFFORT", catalog.DEFAULT_EFFORT), help="low, medium, high, xhigh or max")
    parser.add_argument("--context-tokens", type=int, default=int(env.get("AGENT_CONTEXT_TOKENS", "0") or 0), help="context window to assume; 0 uses the model catalog")
    parser.add_argument("--skill", default=env.get("AGENT_SKILL", ""), help="skill name to activate ('none' for no skill)")
    parser.add_argument("--skills-dir", action="append", default=[], help="directory holding <skill>/skill.md; repeatable")
    parser.add_argument("--workspace", default=env.get("AGENT_WORKSPACE", os.getcwd()), help="the agent's working directory")
    parser.add_argument("--state-dir", default=env.get("AGENT_STATE_DIR", ""), help="where sessions and state files live (default: <workspace>/.agent)")
    parser.add_argument("--session-id", default=env.get("AGENT_SESSION_ID", ""), help="session identifier (default: BUILD_TAG or a timestamp)")
    parser.add_argument("--max-iterations", type=int, default=int(env.get("AGENT_MAX_ITERATIONS", "200") or 200))
    parser.add_argument("--dry-run", action="store_true", default=env.get("AGENT_DRY_RUN", "").lower() in ("1", "true", "yes"))
    parser.add_argument("--deny-file", default=env.get("AGENT_DENY_FILE", ""), help="file with one deny glob per line")
    parser.add_argument("--deny", action="append", default=[], help="additional deny glob; repeatable")
    parser.add_argument("--secret-env", action="append", default=[], help="environment variable whose value must be masked; repeatable")
    parser.add_argument("--hide-env", action="append", default=[], help="environment variable hidden from tool subprocesses; repeatable")
    parser.add_argument("--bash-timeout", type=int, default=int(env.get("AGENT_BASH_TIMEOUT", "600") or 600))
    parser.add_argument("--max-bash-timeout", type=int, default=int(env.get("AGENT_MAX_BASH_TIMEOUT", "3600") or 3600))
    parser.add_argument("--shell", default=env.get("AGENT_SHELL", ""), help="shell binary for the bash tool")
    parser.add_argument("--http-timeout", type=float, default=float(env.get("AGENT_HTTP_TIMEOUT", "600") or 600))
    parser.add_argument("--fake-script", default=env.get("AGENT_FAKE_SCRIPT", ""), help="JSON reply script for the fake provider")
    parser.add_argument("--allow-unknown-model", action="store_true")
    parser.add_argument("--git-host", default=env.get("GIT_SERVER_HOST", ""), help="git server host mentioned in the prompt")
    parser.add_argument("--instructions-file", action="append", default=[], help="extra system prompt text; repeatable")
    parser.add_argument("--verbose", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pipeline_agent", description="Run an AI agent skill inside a Jenkins build.")
    parser.add_argument("--version", action="version", version=f"pipeline_agent {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="start a new session with a prompt")
    run.add_argument("--prompt", default="", help="the task text")
    run.add_argument("--prompt-file", default="", help="file holding the task text")
    add_common_arguments(run)
    run.set_defaults(func=cmd_run)

    resume = subparsers.add_parser("resume", help="continue an existing session")
    resume.add_argument("--answers-file", default="", help="properties file with Q<n>=answer lines")
    resume.add_argument("--more-iterations", type=int, default=0, help="raise the iteration limit by this many")
    add_common_arguments(resume)
    resume.set_defaults(func=cmd_resume)

    state = subparsers.add_parser("state", help="print the last state")
    state.add_argument("--state-dir", default=os.environ.get("AGENT_STATE_DIR", ""))
    state.add_argument("--workspace", default=os.environ.get("AGENT_WORKSPACE", os.getcwd()))
    state.add_argument("--field", default="", help="print one property value only")
    state.add_argument("--properties", action="store_true", help="print the properties rendering")
    state.set_defaults(func=cmd_state)

    check = subparsers.add_parser("check", help="validate the configuration; optionally call the model once")
    check.add_argument("--probe", action="store_true", help="send a one-line request to the model")
    add_common_arguments(check)
    check.set_defaults(func=cmd_check)

    skills = subparsers.add_parser("skills", help="list available skills")
    skills.add_argument("--skills-dir", action="append", default=[])
    skills.set_defaults(func=cmd_skills)

    prompt = subparsers.add_parser("prompt", help="print the system prompt that would be used")
    add_common_arguments(prompt)
    prompt.set_defaults(func=cmd_prompt)
    return parser


# --- assembly -----------------------------------------------------------------


def _skills_dirs(args: argparse.Namespace) -> list[Path]:
    dirs = [Path(d) for d in (args.skills_dir or []) if d]
    env_dirs = os.environ.get("AGENT_SKILLS_DIRS", "")
    dirs.extend(Path(d) for d in env_dirs.split(os.pathsep) if d.strip())
    if not dirs:
        dirs.append(DEFAULT_SKILLS_DIR)
    return dirs


def _state_dir(args: argparse.Namespace, workspace: Path) -> Path:
    return Path(args.state_dir).resolve() if getattr(args, "state_dir", "") else (workspace / ".agent")


def _session_id(args: argparse.Namespace) -> str:
    raw = getattr(args, "session_id", "") or os.environ.get("BUILD_TAG", "") or f"local-{time.strftime('%Y%m%d-%H%M%S')}"
    return safe_session_id(raw)


def _shell(args: argparse.Namespace) -> str:
    if args.shell:
        return args.shell
    for candidate in ("/bin/bash", "/usr/bin/bash"):
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("bash")
    if found:
        return found
    raise UsageError("bash was not found; pass --shell")


def build_runtime(args: argparse.Namespace) -> Runtime:
    workspace = Path(args.workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = _state_dir(args, workspace)
    state_dir.mkdir(parents=True, exist_ok=True)

    secret_names = list(DEFAULT_SECRET_ENV) + list(args.secret_env or [])
    hidden_names = set(DEFAULT_HIDDEN_ENV) | set(args.hide_env or [])
    redactor = Redactor(os.environ.get(name, "") for name in secret_names)
    console = Console(redactor, verbose=args.verbose)

    try:
        settings = ProviderSettings(
            provider=args.provider,
            model=args.model,
            effort=args.effort,
            timeout=args.http_timeout,
            fake_script=args.fake_script or None,
            allow_unknown_model=args.allow_unknown_model,
        )
        info = validate_model(settings)
        provider = build_provider(settings, os.environ, redactor)
    except (ValueError, AuthenticationError) as exc:
        raise UsageError(str(exc)) from exc

    window = info.context_window
    if args.context_tokens and args.context_tokens > 0:
        window = min(args.context_tokens, info.context_window) if catalog.lookup(settings.provider, settings.model) else args.context_tokens
    reserve = min(info.reserve_tokens, window // 4)
    budget = catalog.context_budget(window, reserve)

    skill_dirs = _skills_dirs(args)
    skill: Optional[Skill] = None
    if args.skill and args.skill.strip().lower() != "none":
        skill = find_skill(skill_dirs, args.skill)
        if skill is None:
            available = ", ".join(sorted(discover_skills(skill_dirs).keys())) or "(none)"
            raise UsageError(f"skill '{args.skill}' was not found in {', '.join(str(d) for d in skill_dirs)}; available: {available}")

    extra_instructions: list[str] = []
    for path in args.instructions_file or []:
        extra_instructions.append(Path(path).read_text(encoding="utf-8"))

    registry: ToolRegistry = default_registry()
    prompt_ctx = PromptContext(
        workspace=workspace,
        provider=settings.provider,
        model=settings.model,
        tool_names=registry.names(),
        dry_run=args.dry_run,
        git_host=args.git_host,
        job_name=os.environ.get("JOB_NAME", ""),
        build_number=os.environ.get("BUILD_NUMBER", ""),
        build_url=os.environ.get("BUILD_URL", ""),
        default_timeout=args.bash_timeout,
        max_timeout=args.max_bash_timeout,
        extra_instructions=extra_instructions,
    )
    system_prompt = build_system_prompt(prompt_ctx, skill)

    deny_file = args.deny_file or (str(DEFAULT_DENY_FILE) if DEFAULT_DENY_FILE.exists() else "")
    policy = CommandPolicy.build(deny_file=deny_file or None, extra_patterns=args.deny or [], dry_run=args.dry_run)
    shell = _shell(args)
    tool_env = {key: value for key, value in os.environ.items() if key not in hidden_names}
    tool_env.setdefault("GIT_TERMINAL_PROMPT", "0")
    tool_env.setdefault("PYTHONUNBUFFERED", "1")

    store = SessionStore(state_dir / "sessions")
    context = ContextManager(provider, ContextSettings(budget_tokens=budget), store=store, console=console)
    archive_dir = state_dir / "tool-output"

    def tool_context(session_id: str, call_id: str) -> ToolContext:
        return ToolContext(
            workspace=workspace,
            call_id=call_id,
            session_id=session_id,
            archive_dir=archive_dir,
            redactor=redactor,
            policy=policy,
            shell=shell,
            default_timeout=args.bash_timeout,
            max_timeout=args.max_bash_timeout,
            tool_env=tool_env,
            console=console,
        )

    loop = AgentLoop(
        provider=provider,
        registry=registry,
        store=store,
        context=context,
        system_prompt=system_prompt,
        tool_context_factory=tool_context,
        console=console,
        settings=LoopSettings(max_iterations=max(1, args.max_iterations)),
    )
    return Runtime(
        provider=provider,
        loop=loop,
        store=store,
        console=console,
        redactor=redactor,
        workspace=workspace,
        state_dir=state_dir,
        skill=skill,
        system_prompt=system_prompt,
        model_window=window,
        budget_tokens=budget,
        settings=settings,
    )


def _banner(runtime: Runtime, args: argparse.Namespace, session_id: str) -> None:
    runtime.console.info(
        "\n".join(
            [
                f"pipeline_agent {__version__}",
                f"provider: {runtime.settings.provider}  model: {runtime.settings.model}  thinking: {runtime.settings.effort}",
                f"context window: {runtime.model_window:,} tokens  prompt budget: {runtime.budget_tokens:,} tokens",
                f"skill: {runtime.skill.name if runtime.skill else '(none)'}",
                f"workspace: {runtime.workspace}",
                f"state: {runtime.state_dir}",
                f"session: {session_id}  max iterations: {args.max_iterations}  dry run: {'yes' if args.dry_run else 'no'}",
            ]
        )
    )


def _finish(runtime: Runtime, outcome: RunOutcome) -> int:
    write_state(runtime.state_dir, outcome)
    runtime.console.summary(outcome.status, outcome.session)
    if outcome.status == "waiting_for_question":
        runtime.console.info(f"state written to {runtime.state_dir}; answer the questions and run 'resume --answers-file'")
    elif outcome.final_text:
        runtime.console.info("final report saved to " + str(runtime.state_dir / "final_message.md"))
    return exit_code_for(outcome.status)


def _write_failure_state(args: argparse.Namespace, message: str) -> None:
    try:
        workspace = Path(args.workspace).resolve()
        state_dir = _state_dir(args, workspace)
        session = Session(session_id=_session_id(args), status=STATUS_ERROR, last_error=message)
        write_state(state_dir, RunOutcome(status=STATUS_ERROR, session=session, error=message))
    except Exception:  # noqa: BLE001 - best effort only
        pass


# --- commands -----------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    prompt = args.prompt or ""
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8")
    if not prompt.strip():
        raise UsageError("a prompt is required (--prompt or --prompt-file)")
    runtime = build_runtime(args)
    session_id = _session_id(args)
    _banner(runtime, args, session_id)
    if runtime.store.exists(session_id):
        runtime.console.warn(f"session {session_id} already exists and will be replaced")
    runtime.console.info("task:\n" + prompt.strip())
    info = {
        "provider": runtime.settings.provider,
        "model": runtime.settings.model,
        "effort": runtime.settings.effort,
        "skill": runtime.skill.name if runtime.skill else "",
        "workspace": str(runtime.workspace),
        "dry_run": bool(args.dry_run),
    }
    outcome = runtime.loop.start(session_id, prompt.strip(), info)
    return _finish(runtime, outcome)


def cmd_resume(args: argparse.Namespace) -> int:
    runtime = build_runtime(args)
    session_id = _session_id(args)
    if not runtime.store.exists(session_id):
        raise UsageError(f"no session named {session_id} under {runtime.store.root}")
    _banner(runtime, args, session_id)
    answers = parse_answers_file(args.answers_file) if args.answers_file else None
    outcome = runtime.loop.resume(session_id, answers=answers, more_iterations=args.more_iterations)
    return _finish(runtime, outcome)


def cmd_state(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    state = read_state(_state_dir(args, workspace))
    if args.field:
        value = state_field(state, args.field) if state else None
        print("" if value is None else value)
        return 0
    if args.properties or not state:
        print(render_properties(state) if state else "", end="")
        return 0
    import json

    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    runtime = build_runtime(args)
    session_id = _session_id(args)
    _banner(runtime, args, session_id)
    token_source = getattr(runtime.provider, "token_source", None)
    if token_source:
        runtime.console.info(f"credential type: {token_source}")
    runtime.console.info("configuration looks valid")
    if args.probe:
        runtime.console.info("probing the model with a one-line request")
        try:
            reply = runtime.provider.complete(
                system="You are a connectivity probe. Reply with exactly the word: ok",
                messages=[Message(role="user", content="ping")],
                tools=[],
            )
        except ProviderError as exc:
            runtime.console.error(f"probe failed: {exc}")
            return 1
        runtime.console.info(f"probe reply: {reply.text!r} usage: {reply.usage}")
    return 0


def cmd_skills(args: argparse.Namespace) -> int:
    skills = discover_skills(_skills_dirs(args))
    if not skills:
        print("(no skills found)")
        return 0
    for name in sorted(skills):
        skill = skills[name]
        print(f"{skill.name}\t{skill.description}")
    return 0


def cmd_prompt(args: argparse.Namespace) -> int:
    runtime = build_runtime(args)
    print(runtime.system_prompt)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        _write_failure_state(args, str(exc))
        return EXIT_USAGE
    except (ProviderError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        _write_failure_state(args, str(exc))
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


__all__ = ["build_parser", "build_runtime", "main"]
