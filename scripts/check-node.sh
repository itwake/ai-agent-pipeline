#!/usr/bin/env bash
# Sanity check of the build node before the agent starts: Python version and
# the tools the skills rely on. Only Python and git are mandatory.
set -u

status=0
echo "node: $(uname -a)"
echo "PATH: $PATH"

if command -v python3 >/dev/null 2>&1; then
  echo "python3: $(command -v python3) ($(python3 --version 2>&1))"
  if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    echo "ERROR: Python 3.11 or newer is required" >&2
    status=1
  fi
else
  echo "ERROR: python3 not found on PATH" >&2
  status=1
fi

if command -v git >/dev/null 2>&1; then
  echo "git: $(command -v git) ($(git --version 2>&1))"
else
  echo "ERROR: git not found on PATH" >&2
  status=1
fi

for tool in bash java mvn gradle gh jq yq node nexus-iq-cli; do
  if command -v "$tool" >/dev/null 2>&1; then
    echo "$tool: $(command -v "$tool")"
  else
    echo "$tool: not found (fine unless the skill needs it)"
  fi
done

if [ -n "${JAVA_HOME:-}" ]; then
  echo "JAVA_HOME: $JAVA_HOME"
fi

exit $status
