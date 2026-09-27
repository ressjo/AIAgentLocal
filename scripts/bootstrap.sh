#!/usr/bin/env bash
# JARVIS an einem festen Ort per Git installieren bzw. aktualisieren und dann install.sh starten.
#
#   bash bootstrap.sh            # nach ~/jarvis
#   JARVIS_HOME=~/apps/jarvis bash bootstrap.sh
set -euo pipefail

REPO="${JARVIS_REPO:-https://github.com/ressjo/AIAgentLocal.git}"
BRANCH="${JARVIS_BRANCH:-claude/epic-volta-vyvxlk}"
TARGET="${JARVIS_HOME:-$HOME/jarvis}"

command -v git >/dev/null || sudo pacman -S --needed --noconfirm git

if [ -d "$TARGET/.git" ]; then
  echo "==> Aktualisiere $TARGET"
  git -C "$TARGET" fetch origin "$BRANCH"
  git -C "$TARGET" checkout -q "$BRANCH"
  git -C "$TARGET" pull --ff-only origin "$BRANCH"
elif [ -e "$TARGET" ] && [ -n "$(ls -A "$TARGET" 2>/dev/null)" ]; then
  echo "$TARGET existiert bereits und ist kein Git-Checkout – bitte umbenennen oder JARVIS_HOME setzen." >&2
  exit 1
else
  echo "==> Lade JARVIS nach $TARGET"
  git clone -b "$BRANCH" "$REPO" "$TARGET"
fi

exec "$TARGET/scripts/install.sh" "$@"
