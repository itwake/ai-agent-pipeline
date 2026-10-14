---
name: pipeline-smoke
description: Smoke-test the pipeline wiring without touching any repository: check the tools on the node, exercise bash and the file tools, ask the user one question, and write the final report.
version: 1.0.0
---
# Pipeline smoke test

Purpose: prove that the agent runs on this node, that tools work, that a question round-trips through Jenkins, and that the final report is archived. Do not clone or modify any repository and do not push anything.

## Steps

1. Run these checks with bash and keep the output short (`head` where needed): `uname -a`, `python3 --version`, `git --version`, `java -version 2>&1 | head -3`, `mvn -v 2>&1 | head -3`, `gradle --version 2>&1 | head -5`, `gh --version | head -1`, `jq --version`. A missing tool is a finding, not a failure; note it and continue.
2. Create a scratch file `smoke/hello.txt` with write_file, append a second line with edit_file, and read it back with read_file.
3. Call ask_user with exactly one question: header `Smoke test`, question `The tool checks are done. How should the smoke test finish?`, options `report` (write the report now) and `extra` (also run `env | sort | head -20` first), allow_custom true.
4. Follow the answer. If the user chose `extra`, run the env listing (the output is masked for secrets; that is expected).
5. Write the final report. Under Verification list every tool version found and every tool that was missing.
