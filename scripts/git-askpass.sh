#!/usr/bin/env bash
# Credential helper for non-interactive git over HTTPS.
# Git calls this with a prompt such as "Username for 'https://host': " or
# "Password for 'https://user@host': "; the answers come from the environment
# that Jenkins' withCredentials step populated. The token never touches disk.
case "$1" in
  [Uu]sername*) printf '%s\n' "${GIT_USERNAME:-}" ;;
  *)            printf '%s\n' "${GIT_PASSWORD:-}" ;;
esac
