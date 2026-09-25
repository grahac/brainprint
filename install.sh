#!/bin/bash
# Install brainprint's /decide skill and advisory into Pi, Codex and Claude Code.
# Writes only under $HOME. Never overwrites a file brainprint did not install.
# Run from anywhere: bash install.sh
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$(cd "$(dirname "$0")" && pwd)"
ENGINE="$PYTHONPATH"
SKILL="$ENGINE/skills/decide/SKILL.md"
EXTENSION="$ENGINE/integrations/pi/brainprint-advisory.ts"

# target|source symlinks, for the harnesses that exist under $HOME.
LINKS=()
if [ -d "$HOME/.pi/agent" ]; then
  LINKS+=("$HOME/.pi/agent/skills/decide/SKILL.md|$SKILL")
  LINKS+=("$HOME/.pi/agent/extensions/brainprint-advisory.ts|$EXTENSION")
fi
if [ -d "$HOME/.codex" ]; then LINKS+=("$HOME/.codex/skills/decide/SKILL.md|$SKILL"); fi
if [ -d "$HOME/.agents" ]; then LINKS+=("$HOME/.agents/skills/decide/SKILL.md|$SKILL"); fi
if [ -d "$HOME/.claude" ]; then LINKS+=("$HOME/.claude/commands/decide.md|$SKILL"); fi

fail() { echo "brainprint: $1" >&2; exit 1; }

# 1. Check everything before writing anything.
for link in ${LINKS[@]+"${LINKS[@]}"}; do
  target="${link%%|*}"; source="${link#*|}"
  if [ -L "$target" ] && [ "$(readlink "$target")" = "$source" ]; then continue; fi
  if [ -e "$target" ] || [ -L "$target" ]; then
    fail "refusing to overwrite $target (not installed by brainprint); move it aside and re-run."
  fi
done

CONFIG="$(python3 -c 'from pipeline import config; print(config.config_path())')"
PLUGIN=""
if [ -f "$CONFIG" ]; then
  PLUGIN="$(python3 -c 'from pipeline import config, render; print(render.plugin_dir(config.load()))')"
  HOME_REAL="$(cd "$HOME" && pwd -P)"
  case "$PLUGIN" in
    "$HOME_REAL"/*) ;;
    *) fail "the rendered plugin would land at $PLUGIN, outside \$HOME; set brain_dir under \$HOME." ;;
  esac
fi

# 2. Write.
echo "=== Installing brainprint ==="
if [ -n "$PLUGIN" ]; then
  python3 -m pipeline.render >/dev/null
  echo "✓ advisory rendered: $PLUGIN/advisory.md"
else
  echo "✗ no brain yet ($CONFIG): run \`brainprint init\`, which renders the advisory."
fi
for link in ${LINKS[@]+"${LINKS[@]}"}; do
  target="${link%%|*}"; source="${link#*|}"
  mkdir -p "$(dirname "$target")"
  ln -sfn "$source" "$target"
  echo "✓ $target"
done
if [ ${#LINKS[@]} -eq 0 ]; then echo "✗ no ~/.pi/agent, ~/.codex, ~/.agents or ~/.claude found"; fi

cat <<EOF

--- Not done for you ---
Claude Code / Codex advisory: register the rendered plugin (once; refresh updates it in place):
  claude plugin marketplace add "${PLUGIN%/plugin}" && claude plugin install brainprint@brainprint-local
  codex plugin marketplace add "${PLUGIN%/plugin}" && codex plugin add brainprint@brainprint-local

Optional CLAUDE.md snippet (paste into a project's CLAUDE.md if you want it):

## brainprint
The brainprint advisory gives lightweight recommendations automatically. Treat them as advice, never authority.
\`/decide [context]\` predicts how the brain's owner would decide, from brain/full.md.

Disable the advisory for one session: export BRAINPRINT_ADVISORY=0
EOF
