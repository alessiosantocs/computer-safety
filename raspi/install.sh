#!/usr/bin/env bash
set -euo pipefail

USERS=("amal" "anais" "lelia")
SCRIPT_SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing dependencies (python3, python3-tk)..."
sudo apt-get update -y
sudo apt-get install -y python3 python3-tk

echo "Installing timekeeper to /usr/local/bin/computer-safety-timekeeper"
sudo install -m 0755 "${SCRIPT_SRC_DIR}/timekeeper.py" /usr/local/bin/computer-safety-timekeeper

for u in "${USERS[@]}"; do
  HOME_DIR=$(getent passwd "$u" | cut -d: -f6)
  if [[ -z "$HOME_DIR" || ! -d "$HOME_DIR" ]]; then
    echo "Skipping user $u: home directory not found"
    continue
  fi

  AUTOSTART_DIR="$HOME_DIR/.config/autostart"
  echo "Setting up autostart for user $u in $AUTOSTART_DIR"
  sudo -u "$u" mkdir -p "$AUTOSTART_DIR"
  sudo install -m 0644 "${SCRIPT_SRC_DIR}/computer-safety.desktop" "$AUTOSTART_DIR/computer-safety.desktop"
  sudo chown "$u":"$u" "$AUTOSTART_DIR/computer-safety.desktop"
done

echo "Done. Log out and back in as a kid account to test."

