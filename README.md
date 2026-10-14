# ai-agent-pipeline

A Jenkins pipeline that runs an AI agent on a Linux build node. You pick the model provider, model, thinking level, context size and a *skill* (a task playbook such as "fix FOSS findings in a Java project"), type a prompt, and the agent does the work with the node's own tools: bash, git, Maven, Gradle and whatever else is on `PATH`. When the agent needs a decision it cannot make itself, the build pauses with a Jenkins **input** form, and continues with the answer.

The agent runtime lives in this repository (`pipeline_agent/`) and uses nothing but the Python standard library, so a node only needs Python 3.11+ and git.

```
Jenkins job (parameters) ──▶ Checkout ──▶ Prepare (PATH, prompt file, node check)
                                               │
                                               ▼
                              ┌──── python3 -m pipeline_agent run ───────────┐
                              │  model call ⇄ tools (bash, read/edit/write)   │
                              │  ... until the reply has no tool call         │
                              └──────────────┬───────────────────────────────┘
                                             │ exit 10 + state.properties
                      agent asked a question ▼
                              Jenkins input form ──▶ answers file ──▶ resume ──▶ ... ──▶ completed
                                                                                         │
                                                                              Report: archive .agent/, print final report
```

## Pipeline parameters

| Parameter | Values | Meaning |
| --- | --- | --- |
| `AI_PROVIDER` | `github_copilot`, `ai_platform`, `fake` | Model service. `fake` replays `config/fake-script.json` without credentials; use it to test the wiring. |
| `MODEL` | `gpt-6-sol`, `gpt-6-astra`, `gpt-6-luna`, `gpt-5.6-sol`, `gpt-5.6-luna`, `gpt-5.6-terra`, `gpt-5.4` | GitHub Copilot serves the `gpt-6-*` and `gpt-5.6-*` models; AI Platform serves `gpt-5.4` and `gpt-5.6-*`. A model the provider does not serve fails in the Validate stage. |
| `THINKING_LEVEL` | `low`, `medium`, `high`, `xhigh`, `max` | Reasoning effort sent with every model call. |
| `CONTEXT_SIZE` | `default`, `128k`, `256k`, `512k`, `1m` | Context window the agent may fill. `default` uses the model's catalog value. A smaller window compacts the conversation earlier and costs less per call. |
| `SKILL` | directory names under `skills/`, or `none` | The playbook injected into the system prompt. |
| `PROMPT` | free text | The task. For `fix-foss`: repository URL, base branch if not the default, and the findings or where the report is. |
| `MAX_ITERATIONS` | number | Model calls allowed before the pipeline asks whether to continue. |
| `INPUT_TIMEOUT_MINUTES` | number | How long a question may wait before the build is aborted. |
| `DRY_RUN` | boolean | The agent does everything except push or open pull requests. |
| `AGENT_LABEL` | node label | Where to run. |

## Jenkins setup

### Plugins

Pipeline, Git, Credentials Binding and **Folder Properties**. Pipeline Utility Steps is not needed; the Jenkinsfile parses the agent's state file with plain string methods and uses no classes that need script approval.

### Credentials

Create these in the credentials store the job can see. The ids are defaults; override them with the Folder Properties below.

| Id | Kind | Content |
| --- | --- | --- |
| `ghe-token` | Username with password | Git server user and personal access token with read/write on the target repositories. Used for clone and push through `GIT_ASKPASS`; the token never touches disk. |
| `copilot-github-token` | Secret text | A GitHub token (`ghp_`, `gho_`, `github_pat_`...) of an account with GitHub Copilot access. It is exchanged for a short-lived Copilot session token at every build. |
| `ai-platform-credentials` | Username with password | AI Platform service account; exchanged for a JWT at the iB2B endpoint. |

### Folder Properties

Set these on the folder that holds the job (all optional unless noted).

| Property | Purpose | Default |
| --- | --- | --- |
| `GIT_CREDENTIALS_ID`, `COPILOT_CREDENTIALS_ID`, `AI_PLATFORM_CREDENTIALS_ID` | Credential ids to use | the ids above |
| `GIT_SERVER_HOST` | Host name of the company git server; told to the agent and exported as `GH_HOST` | empty |
| `GIT_COMMIT_NAME`, `GIT_COMMIT_EMAIL` | Identity for the agent's commits | `ai-agent-pipeline` / `ai-agent-pipeline@noreply.local` |
| `AI_PLATFORM_CHAT_HOST` | Gateway host, e.g. `https://gateway.example.com` (**required for ai_platform**) | |
| `AI_PLATFORM_CHAT_URI` | Chat Completions path | `/v1/api/v1/chat/completions` |
| `AI_PLATFORM_RESPONSES_URI`, `AI_PLATFORM_RESPONSES_HOST` | Responses-style endpoint; when the URI is set it is used instead of chat (host defaults to the chat host) | |
| `AI_PLATFORM_IB2B_HOST`, `AI_PLATFORM_IB2B_URI` | Token-exchange endpoint (**host required for ai_platform**) | `/dsp/token` |
| `AI_PLATFORM_USERCASE` | Use case id sent as the `user` field | |
| `AI_PLATFORM_TRUST_TOKEN_HEADER` | Name of the header carrying the JWT (**required for ai_platform**) | |
| `AI_PLATFORM_TRACKING_PREFIX` | Prefix of the correlation ids | `AGENT` |
| `AI_PLATFORM_TOKEN_TTL_SECONDS` | Lifetime assumed for an issued JWT | `30` |
| `BUILD_TOOLS_ROOT` | Root of the tool installations | `/opt/build_tools` |
| `BUILD_TOOLS` | Comma-separated directories added to `PATH` as `<root>/<tool>/latest/bin` | python, git, jdk, maven, gradle, gh, jq, ... (see the Jenkinsfile) |
| `JAVA_HOME_DIR` | `JAVA_HOME` for the agent | `<root>/jdk/latest` |
| `SKILLS_REPO_URL`, `SKILLS_REPO_REF` | Optional second skills repository cloned with the git credential and added to the skill search path | |

### Node requirements

- Linux, bash, git.
- Python 3.11 or newer on `PATH` as `python3` (the default `BUILD_TOOLS` puts `/opt/build_tools/python/latest/bin` first).
- Whatever the skills need: JDK, Maven, Gradle, and a Maven `settings.xml` that can reach the company repository manager.
- Outbound HTTPS to the model service (`HTTPS_PROXY` is honoured).

### The job

Create a *Pipeline* job (or a multibranch one), point *Pipeline script from SCM* at this repository and leave the script path as `Jenkinsfile`. The first build registers the parameters; builds after that show the form.

## First runs

1. **Wiring test without a model.** Build with `AI_PROVIDER=fake`, `SKILL=pipeline-smoke`. The scripted replies run a few commands, write a file, ask one question (answer it in the input form) and finish. This proves PATH, the state file round trip and the input form.
2. **Real model, no repository.** `AI_PROVIDER=github_copilot`, `SKILL=pipeline-smoke`, prompt `Run the smoke test.` The agent inspects the node, asks how to finish, and writes a report.
3. **A real task.** `SKILL=fix-foss` with a prompt such as:

   ```
   Repository: https://git.example.com/team/service.git (base branch: develop)
   Findings from Nexus IQ:
   - com.fasterxml.jackson.core:jackson-databind 2.13.2 -> fix 2.13.4.2 (CVE-2022-42003)
   - org.apache.commons:commons-text 1.9 -> fix 1.10.0 (CVE-2022-42889)
   Push the fix branch; do not open a PR.
   ```

   Use `DRY_RUN` the first time to review the diff before anything is pushed.

## How a build runs

- **Prepare** writes the prompt to `.agent/prompt.txt`, builds `PATH` from `BUILD_TOOLS`, and runs `scripts/check-node.sh`.
- **Validate** runs `python3 -m pipeline_agent check`: provider, model, skill and credentials are checked before any model call.
- **Agent** loops over `python3 -m pipeline_agent run|resume ...`. Each process drives the agent until it completes, asks a question, hits the iteration limit, or fails, then writes `.agent/state.properties` (flat `KEY=value`, newlines escaped as `\n`) and exits with a status code: 0 completed, 10 waiting for a question, 12 iteration limit, 1 error, 2 configuration error. Questions become a Jenkins `input` form (one choice or text field per question, plus a free-text field when the agent allows it); the answers are written to `.agent/answers-<n>.properties` and the session is resumed.
- **Report** archives `.agent/` (session, append-only transcript, every tool output in full, state files, the final report) and prints the agent's final report.

The agent keeps its working files under `agent-ws/` in the job workspace; the target repository is cloned there by the agent itself.

## Running the agent by hand

```bash
export COPILOT_GITHUB_TOKEN=...            # or the AI_PLATFORM_* variables
python3 -m pipeline_agent check --probe --provider github_copilot --model gpt-6-sol
python3 -m pipeline_agent run --provider github_copilot --model gpt-6-sol --effort high \
    --skill fix-foss --workspace /tmp/agent-ws --prompt-file task.txt
# exit code 10: look at /tmp/agent-ws/.agent/state.properties, write answers, then
python3 -m pipeline_agent resume --workspace /tmp/agent-ws --answers-file answers.properties
python3 -m pipeline_agent state --workspace /tmp/agent-ws --field STATUS
python3 -m pipeline_agent skills
python3 -m pipeline_agent prompt --skill fix-foss        # print the system prompt
```

`python3 -m pipeline_agent run --help` lists every option. Most options also read an `AGENT_*` environment variable (`AGENT_PROVIDER`, `AGENT_MODEL`, `AGENT_EFFORT`, `AGENT_SKILL`, `AGENT_WORKSPACE`, `AGENT_STATE_DIR`, `AGENT_MAX_ITERATIONS`, `AGENT_DRY_RUN`, ...).

## Skills

A skill is a directory under `skills/` with a `skill.md`: YAML-style frontmatter (`name`, `description`) followed by the playbook in Markdown. The whole file is placed in the system prompt, and the directory path is given to the agent so it can read reference files or run scripts kept next to it. See [docs/skills.md](docs/skills.md) for the format and for how to try a skill locally. Bundled skills:

| Skill | Purpose |
| --- | --- |
| `fix-foss` | Upgrade flagged Maven/Gradle dependencies in a Java project, build, test, commit and push a fix branch. |
| `pipeline-smoke` | Exercise the node, the tools and the question round trip without touching any repository. |

To offer a new skill in the job, add its name to the `SKILL` choice in the Jenkinsfile.

## Safety

- **Command policy.** Force pushes, deploys, publishes, deletes, privilege escalation and a few other patterns are refused before they run (`pipeline_agent/tools/permissions.py`); site-specific additions go in `config/command-deny.txt`. `DRY_RUN` additionally blocks `git push` and pull request creation.
- **Secrets.** Model credentials are removed from the environment the agent's commands see, and every known secret value (plus token-shaped strings) is masked in tool output, in the transcript and in the console log. The git token reaches git only through `GIT_ASKPASS`.
- **Workspace containment.** The file tools and the bash `workdir` cannot leave the agent workspace.
- **Budgets.** `MAX_ITERATIONS` caps model calls (the build asks before granting more); every bash call has a timeout (default 600 s, at most 3600 s); the conversation is pruned and summarised to stay inside `CONTEXT_SIZE`; an identical tool call repeated three times in a row is refused.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| Validate fails with "model ... is not available for ..." | The `MODEL` choice does not belong to the chosen provider. |
| Validate fails with "GitHub Copilot token exchange failed (HTTP 401/403)" | The token is invalid or the account has no Copilot access. |
| "AI Platform configuration is incomplete; set ..." | A required Folder Property is missing. |
| Builds stall at a `mvn`/`gradle` call that is killed after 600 s | The agent did not set `timeout_seconds`; the prompt tells it to, but you can raise the default with `--bash-timeout` in the Jenkinsfile. |
| `git clone` prompts for credentials or fails with 401 | Check `GIT_CREDENTIALS_ID`; the username must be set together with the token. |
| The input form shows `[other: see text answer]` | Pick it and fill the free-text field below it, or just fill the text field. |

## Development

```bash
python3 -m unittest discover -s tests -t .
```

The tests cover the tools, the loop (pause/resume, limits, repeated-call guard), context compaction, the state files, both providers against a local fake HTTP server, and the command line end to end with the scripted provider. No network access is needed.
