#!/usr/bin/env bash
set -euo pipefail

# Users to clean up (edit if needed)
USERS=("amal" "anais" "lelia")

echo "Stopping any running agents..."
sudo pkill -f computer-safety-timekeeper || true

echo "Removing installed binary..."
sudo rm -f /usr/local/bin/computer-safety-timekeeper || true

echo "Removing per-user autostart entries and data..."
for u in "${USERS[@]}"; do
  HOME_DIR=$(getent passwd "$u" | cut -d: -f6)
  [[ -z "$HOME_DIR" || ! -d "$HOME_DIR" ]] && { echo "Skip $u (no home)"; continue; }
  sudo rm -f "$HOME_DIR/.config/autostart/computer-safety.desktop" || true
  sudo rm -rf "$HOME_DIR/.local/share/computer-safety" || true
done

echo "Done. You may log out or reboot to complete cleanup."


