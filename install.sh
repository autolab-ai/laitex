#!/bin/sh
# Install texloop and its agent skill:
#   curl -LsSf https://raw.githubusercontent.com/autolab-ai/texloop/main/install.sh | sh
set -e
SRC="${TEXLOOP_SRC:-git+https://github.com/autolab-ai/texloop}"

if command -v uv >/dev/null 2>&1; then
  uv tool install --force "$SRC"
elif command -v pipx >/dev/null 2>&1; then
  pipx install --force "$SRC"
else
  python3 -m pip install --user --upgrade "$SRC"
fi

if ! command -v texloop >/dev/null 2>&1; then
  echo "texloop is installed but not on PATH. Add the tool directory to PATH"
  echo "(for uv: 'uv tool update-shell'), then run: texloop install-skill"
  exit 1
fi

# Add the skill for every agent found on this machine (Claude Code by default).
installed=0
if [ -d "$HOME/.codex" ]; then texloop install-skill --dir "$HOME/.codex/skills"; installed=1; fi
if [ -d "$HOME/.claude" ] || [ "$installed" = 0 ]; then texloop install-skill; fi
echo
echo "Done. Ask your agent: \"Open paper.pdf in texloop, I want to review it.\""
