# Architecture

## Components

```
pipeline_agent/
├── cli.py            command line: run / resume / state / check / skills / prompt
├── loop.py           the agent loop: model call → tool calls → ... ; pause on questions
├── context.py        keeps the conversation inside the context budget (prune, summarise)
├── prompt.py         system prompt (role, environment, rules, final report, skill)
├── skills.py         skill discovery and rendering
├── session.py        Message/Session model, JSON session store, append-only transcript
├── state.py          state.properties / state.json for the pipeline, answers file parsing
├── console.py        progress lines for the Jenkins console
├── redaction.py      secret masking
├── providers/
│   ├── base.py       Provider protocol, ModelReply, error classes
│   ├── http.py       urllib JSON client, HTTP → error mapping
│   ├── openai_format.py  conversation → Responses / Chat Completions payloads; reply parsing
│   ├── copilot.py    GitHub Copilot (token exchange + Responses API)
│   ├── ai_platform.py AI Platform (iB2B JWT exchange + chat or responses endpoint)
│   ├── fake.py       scripted replies for tests and wiring checks
│   └── catalog.py    providers, models, context windows, effort levels
└── tools/
    ├── bash.py       shell execution with timeout, truncation, archive
    ├── files.py      read_file, write_file, edit_file
    ├── ask_user.py   the question tool
    └── permissions.py command deny list
```

## One loop step

1. `cli.build_runtime` assembles the provider, the tool registry, the system prompt (with the active skill), the session store under `<state-dir>/sessions`, and the context manager with a token budget derived from the model's window (`CONTEXT_SIZE`) minus the reply reserve and a safety margin.
2. `AgentLoop.start` creates the session with the user prompt; `AgentLoop.resume` loads it and, when answers are supplied, turns the pending `ask_user` call into a tool result.
3. `_drive` repeats: run every tool call of the last assistant message that has no result yet; stop if the iteration limit is reached; shrink the context if needed; call the model; append the reply; finish when the reply carries no tool call.
4. A tool call to `ask_user` stores the questions in the session (`pending_question`), saves, and returns the status `waiting_for_question`. The next `resume` with answers appends the tool result and carries on with any remaining calls of that turn.
5. Every message, question, resume and compaction is appended to `<session>.transcript.jsonl`; the session file itself is rewritten after each change so a killed process loses at most the call in flight. Unfinished tool calls are re-run on resume.

## Context management

Token counts are estimated at four characters per token. When a request exceeds the budget:

1. **Prune** tool outputs older than the last 12 messages to a short head plus a pointer to the archived full output.
2. **Summarise** everything between the task prompt and the recent tail with a model call (`context.SUMMARY_SYSTEM_PROMPT`), replacing those messages with one summary message. Assistant/tool pairs are never split.
3. **Hard prune** every tool output when even that is not enough.

A provider error that reports a context overflow triggers the summary step directly and the request is retried once.

## Providers

Both providers speak OpenAI-style JSON over HTTPS using `urllib`, so there is no SDK dependency and `HTTPS_PROXY` works out of the box.

- **GitHub Copilot.** A GitHub token is exchanged at `GET <github api>/copilot_internal/v2/token` for a session token (refreshed before expiry and once on a 401). Requests go to `<api host>/responses` with the Responses API shape: `input` items, `instructions` for the system prompt, function tools, `reasoning.effort`. The API host comes from the exchange response (`endpoints.api` or the `proxy-ep` hint inside the token).
- **AI Platform.** Username and password are exchanged at the iB2B endpoint for a JWT that is sent in a configurable trust-token header together with `x-correlation-id` / `x-usersession-id`; a Responses-style endpoint also receives it as a Bearer token. The payload is Chat Completions (`messages`, `tools[].function`, `reasoning_effort`, `user`) or Responses depending on the configured URI.
- **fake.** Replays a JSON list of replies; a cursor file next to the script lets consecutive processes continue it.

Errors are classified into transient (retried with backoff), authentication, context overflow (compaction + retry) and plain provider errors (the run ends with status `error`).

## State exchanged with Jenkins

| File | Content |
| --- | --- |
| `.agent/state.properties` | `STATUS`, `EXIT_CODE`, `ITERATIONS`, `MODEL_CALLS`, `INPUT_TOKENS`, `OUTPUT_TOKENS`, `ERROR`, `FINAL_MESSAGE`, `QUESTION_COUNT`, `Q<i>_HEADER`, `Q<i>_TEXT`, `Q<i>_CUSTOM`, `Q<i>_OPTION_COUNT`, `Q<i>_OPT<j>_LABEL`, `Q<i>_OPT<j>_DESC` |
| `.agent/state.json` | the same, structured |
| `.agent/final_message.md` | the agent's final report |
| `.agent/questions.json` | the pending questions |
| `.agent/answers-<n>.properties` | written by the pipeline: `Q<i>=<choice>` and optional `Q<i>_TEXT=<free text>` |
| `.agent/sessions/<id>.json` | the live session |
| `.agent/sessions/<id>.transcript.jsonl` | everything that happened, including compacted messages |
| `.agent/tool-output/<call id>.txt` | full output of every bash call |

Exit codes: 0 completed, 10 waiting for a question, 12 iteration limit reached, 1 error, 2 usage or configuration error.

## Security model

- The agent runs as the Jenkins user on the node with the node's full bash; the deny list (`tools/permissions.py`, `config/command-deny.txt`) is the guard rail, not a sandbox.
- Model credentials are stripped from the environment of tool subprocesses; git credentials stay available for `GIT_ASKPASS` and their values are masked in all output.
- File tools and `workdir` are confined to the workspace; the system prompt forbids leaving it, and the deny list blocks the usual ways of damaging the node.
