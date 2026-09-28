#!/usr/bin/env bash
# Install or update Orbwise as a git checkout in a fixed place, then run install.sh.
#
#   bash bootstrap.sh            # → ~/orbwise
#   ORBWISE_HOME=~/apps/orbwise bash bootstrap.sh
#   bash bootstrap.sh --lang en  # all options are passed on to install.sh
set -euo pipefail

REPO="${ORBWISE_REPO:-https://github.com/ressjo/orbwise.git}"
BRANCH="${ORBWISE_BRANCH:-main}"
TARGET="${ORBWISE_HOME:-$HOME/orbwise}"

if ! command -v git >/dev/null; then
  if command -v pacman >/dev/null; then sudo pacman -S --needed --noconfirm git; else sudo apt-get install -y git; fi
fi

if [ -d "$TARGET/.git" ]; then
  echo "==> Updating $TARGET"
  git -C "$TARGET" fetch origin "$BRANCH"
  git -C "$TARGET" checkout -q "$BRANCH"
  git -C "$TARGET" pull --ff-only origin "$BRANCH"
elif [ -e "$TARGET" ] && [ -n "$(ls -A "$TARGET" 2>/dev/null)" ]; then
  echo "$TARGET already exists and is not a git checkout – rename it or set ORBWISE_HOME." >&2
  exit 1
else
  echo "==> Downloading Orbwise to $TARGET"
  git clone -b "$BRANCH" "$REPO" "$TARGET"
fi

exec "$TARGET/scripts/install.sh" "$@"
