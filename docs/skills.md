# Writing skills

A skill is a playbook the agent follows for one kind of task. It is a directory under `skills/` (or under any directory passed with `--skills-dir`) containing `skill.md`:

```
skills/
└── fix-foss/
    ├── skill.md          required
    ├── references/       optional reference material the agent can read
    └── scripts/          optional helper scripts the agent can run with bash
```

## skill.md format

```markdown
---
name: fix-foss
description: One or two sentences saying what the skill does and when to use it.
version: 1.0.0
---
# Title

The playbook: inputs to extract from the prompt, the procedure step by step,
when to ask the user, rules, and what the final report must contain.
```

- `name` is what the `SKILL` parameter and `--skill` match (case-insensitive). It should equal the directory name.
- `description` is required. Other frontmatter keys are kept as metadata and ignored by the runtime. Lists and nested values in the frontmatter are ignored.
- The body is inserted verbatim into the system prompt after the general rules, together with the absolute path of the skill directory and a listing of its files, so the agent can `read_file` a reference or run `scripts/x.sh` with bash.

## Writing guidance

- Say what to extract from the prompt and what to do when something is missing (ask once, with options, through `ask_user`).
- Give the procedure as numbered steps with the concrete commands you expect (`mvn -B -q package`, `./gradlew test`); say which ones need a long `timeout_seconds`.
- State the decision rules so the agent does not have to ask: version selection, branch naming, commit message format.
- List the things it must never do for this task (deploy, downgrade, disable tests).
- Describe the final report: the general template has Summary / Changes / Verification / Open items; add the table or details reviewers need.
- Keep it under roughly 150 lines. Long reference material goes into files in the skill directory that the agent reads on demand.

## Trying a skill

Without a model, with scripted replies (see `config/fake-script.json` for the format):

```bash
python3 -m pipeline_agent run --provider fake --fake-script my-script.json \
    --skill my-skill --workspace /tmp/ws --prompt "..."
```

With a real model, from a shell with the credentials exported:

```bash
export COPILOT_GITHUB_TOKEN=...
python3 -m pipeline_agent prompt --skill my-skill            # review the system prompt
python3 -m pipeline_agent run --skill my-skill --workspace /tmp/ws --dry-run --prompt-file task.txt
```

Watch the console for `[question]` lines; the run exits with code 10 and the questions are in `/tmp/ws/.agent/state.properties`. Write `Q0=...` lines to a file and `resume --answers-file` it.

Add the skill name to the `SKILL` choice list in the Jenkinsfile to offer it in the job.
