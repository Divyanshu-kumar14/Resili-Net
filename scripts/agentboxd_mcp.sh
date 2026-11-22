#!/usr/bin/env bash
# Start the Agentboxd MCP server using credentials from `.env` (by reference).
#
# The key is passed only through the child process environment and is never
# printed, logged, or committed. Resolution order for the key:
#   AGENTBOXD_API_KEY > RESILINET_AGENTBOXD_API_KEY (both read from the
#   current environment first, then from the repo `.env` file).
# The base URL resolves the same way, defaulting to https://api.agentboxd.com.
#
# Point any stdio MCP client at this script instead of hardcoding the key:
#   { "mcpServers": { "agentboxd": {
#       "command": "<repo>/scripts/agentboxd_mcp.sh", "args": [] } } }
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

load_dotenv() {
  # shellcheck disable=SC1090
  [ -f "$ROOT/.env" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in
      ''|\#*) continue ;;
      export\ *) line="${line#export }" ;;
    esac
    case "$line" in
      AGENTBOXD_API_KEY=*|RESILINET_AGENTBOXD_API_KEY=*|AGENTBOXD_BASE_URL=*|RESILINET_AGENTBOXD_BASE_URL=*)
        key="${line%%=*}"
        value="${line#*=}"
        value="${value%\"}"; value="${value#\"}"
        value="${value%\'}"; value="${value#\'}"
        if [ -z "${!key:-}" ]; then
          printf -v "$key" '%s' "$value"
          export "$key"
        fi
        ;;
    esac
  done < "$ROOT/.env"
}

load_dotenv

export AGENTBOXD_API_KEY="${AGENTBOXD_API_KEY:-${RESILINET_AGENTBOXD_API_KEY:-}}"
export AGENTBOXD_BASE_URL="${AGENTBOXD_BASE_URL:-${RESILINET_AGENTBOXD_BASE_URL:-https://api.agentboxd.com}}"

if [ -z "$AGENTBOXD_API_KEY" ]; then
  echo "agentboxd_mcp: no AGENTBOXD_API_KEY (or RESILINET_AGENTBOXD_API_KEY) in environment or .env" >&2
  echo "agentboxd_mcp: copy the mr_... key from https://agentboxd.com/app/api-keys into .env" >&2
  exit 2
fi

exec npx -y @agentboxd/mcp "$@"
