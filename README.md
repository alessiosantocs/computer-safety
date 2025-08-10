Computer Safety (Raspberry Pi MVP)

A very simple per-user timekeeper for Raspberry Pi OS. Each kid gets 30 minutes per day. Time counts from login to logout. When time is up, the session is terminated.

## Features
- Per-user daily limit: 30 minutes
- Fullscreen modal on login asks for session goal (category + plan)
- Shows remaining time countdown
- Logs session start/end locally under `~/.local/share/computer-safety/logs/`
- Resets usage at local midnight

## Requirements
- Raspberry Pi OS desktop (LXDE/PIXEL)
- Python 3, Tkinter

## Install (for users: `amal`, `anais`, `lelia`)
```bash
cd /Users/alessiosanto/projects/computer-safety/raspi
chmod +x install.sh
./install.sh
```

This will:
- Install dependencies (`python3`, `python3-tk`)
- Install the app to `/usr/local/bin/computer-safety-timekeeper`
- Add an autostart entry for the specified users so the app launches at login

## Uninstall
```bash
sudo rm -f /usr/local/bin/computer-safety-timekeeper
for u in amal anais lelia; do
  sudo rm -f "/home/$u/.config/autostart/computer-safety.desktop"
  sudo rm -rf "/home/$u/.local/share/computer-safety"
done
```

## Notes
- The master account is unaffected (no autostart entry installed there).
- If a kid logs in after using the daily time, they will see "Time is up" and will be logged out immediately.
- If the UI is closed, the background process continues ticking and will log out at the limit.
- Set `COMPUTER_SAFETY_DISABLED=1` in the environment to bypass temporarily for troubleshooting.


