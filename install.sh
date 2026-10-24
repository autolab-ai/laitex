#!/bin/sh
# Install pdfloop and its agent skill:
#   curl -LsSf https://raw.githubusercontent.com/autolab-ai/pdfloop/main/install.sh | sh
set -e
SRC="${PDFLOOP_SRC:-git+https://github.com/autolab-ai/pdfloop}"

if command -v uv >/dev/null 2>&1; then
  uv tool install --force "$SRC"
elif command -v pipx >/dev/null 2>&1; then
  pipx install --force "$SRC"
else
  python3 -m pip install --user --upgrade "$SRC"
fi

if ! command -v pdfloop >/dev/null 2>&1; then
  echo "pdfloop is installed but not on PATH. Add the tool directory to PATH"
  echo "(for uv: 'uv tool update-shell'), then run: pdfloop install-skill"
  exit 1
fi

pdfloop install-skill
echo
echo "Done. Ask your agent: \"Open paper.pdf in pdfloop, I want to review it.\""
