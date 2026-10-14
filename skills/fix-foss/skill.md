---
name: fix-foss
description: Fix open-source dependency (FOSS) findings in a Java project: clone the repository, upgrade the flagged Maven or Gradle dependencies to safe versions, make the build and tests pass, and push the fix on a new branch.
version: 1.0.0
---
# Fix FOSS findings in a Java project

You turn a list of FOSS findings (vulnerable or non-compliant open-source dependencies) into a reviewable fix branch. The findings, the repository and any constraints come from the task prompt; everything else you work out from the repository itself.

## Inputs to extract from the prompt

- Repository: an HTTPS URL on the company git server. If the prompt gives only an `org/repo` name, ask the user for the full URL.
- Base branch: use what the prompt says; otherwise the repository's default branch.
- Findings: each is some combination of a dependency coordinate (`groupId:artifactId`), the current version, a CVE or finding id, and a recommended or minimum fixed version. The prompt may instead point at a report file (path inside the workspace or a URL you can fetch with `curl`): read it and extract the same fields. Scanners such as Nexus IQ, Black Duck and OWASP Dependency-Check all name the component and the fixed version; quote them exactly.
- Fix branch name: use what the prompt says; otherwise `foss/fix-<yyyymmdd>` plus the build number when it is known (for example `foss/fix-20260101-42`).

If the prompt names no findings at all and no report, ask the user for them with ask_user before cloning anything.

## Procedure

1. Clone the repository into the workspace (`git clone <url> repo`), check out the base branch, and create the fix branch from it. Use `timeout_seconds` generously for the clone.
2. Learn how the project builds before changing anything:
   - Maven: locate every `pom.xml` (multi-module projects have many), note `<dependencyManagement>`, `<properties>` that hold versions, parent POMs and imported BOMs. Check for a `.mvn/` directory, a wrapper (`./mvnw`) and `settings.xml` hints in the README.
   - Gradle: locate `build.gradle`/`build.gradle.kts`, `settings.gradle`, `gradle/libs.versions.toml` and `gradle.properties`; prefer the wrapper (`./gradlew`) when present.
   - Run the baseline build once (`mvn -B -q -DskipTests package` or `./gradlew build -x test`) with a long timeout so you know the starting state. If the baseline is already broken, say so in the report and still attempt the fix.
3. For every finding, locate where the version is declared:
   - direct dependency with an explicit version: change that version;
   - version held in a property or a version catalog: change it there (one place);
   - transitive dependency only (`mvn dependency:tree -Dincludes=group:artifact` or `./gradlew dependencyInsight --dependency artifact`): pin it with `<dependencyManagement>` in Maven or a `constraints`/`resolutionStrategy` entry in Gradle, and say in the commit message that it is a transitive override;
   - managed by a parent POM or BOM: prefer upgrading the parent or BOM when that is the documented path; otherwise override the single artifact.
   Pick the lowest version that is at or above the fixed version and stays within the same major line when possible. If the fixed version does not exist in the company repository manager, check what is available (`mvn versions:display-dependency-updates`, `nexus-iq-cli`, or `curl` against the repository index) and ask the user when the only options cross a major version boundary.
4. Rebuild and run the tests (`mvn -B test` or `./gradlew test`) with a long timeout. Fix compile errors that follow directly from the upgrade (renamed packages, changed signatures) with minimal edits. Do not rewrite unrelated code, do not disable tests, do not loosen the build. If a test fails for reasons unrelated to the change, record it and move on.
5. Review `git diff`. Commit each finding separately when they touch different files, or one commit for a version-property change that covers several findings. Commit messages follow `fix(deps): upgrade <artifact> <old> -> <new> (<finding id>)`.
6. Push the fix branch (`git push -u origin <branch>`) unless the build runs in dry-run mode. Do not open a pull request unless the prompt asks for one.

## When to ask the user

Ask once, with all open points in a single ask_user call, when:
- a finding cannot be mapped to any dependency in the repository;
- the only available fixed version crosses a major version boundary or changes a public API the project uses;
- two findings demand conflicting versions;
- the build cannot download dependencies at all (repository configuration or credentials), which you cannot fix from inside the build.
Otherwise decide yourself and explain the decision in the report.

## Rules

- Never downgrade a dependency and never change versions that are not part of a finding.
- Keep the project's formatting (indentation, property naming, ordering).
- Never run `mvn deploy`, `mvn release`, or Gradle `publish` tasks.
- Clean up nothing in `~/.m2` or `~/.gradle`; they belong to the node.

## Final report

Use the standard report headings. Under Changes include a table with one row per finding: component, old version, new version, declaration file, commit. Under Verification state the exact build and test commands and whether they passed. Under Open items list every finding that was not fixed and why, and anything the reviewer should check by hand (for example API behaviour changes in a major upgrade).
