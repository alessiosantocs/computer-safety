#!/usr/bin/env python3

import json
import os
import sys
import time
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import ttk


APP_NAME = "computer-safety"
DAILY_LIMIT_SECONDS = 30 * 60  # 30 minutes


def get_username() -> str:
    try:
        return os.getlogin()
    except Exception:
        # Fallback for environments where getlogin() is not available
        return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def get_data_dirs():
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    if xdg_data_home:
        base = Path(xdg_data_home) / APP_NAME
    else:
        base = Path.home() / ".local" / "share" / APP_NAME
    state_dir = base / "state"
    logs_dir = base / "logs"
    state_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    return state_dir, logs_dir


def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class PersistentState:
    """Minimal persistence: per-user daily usage counter."""

    def __init__(self, state_dir: Path):
        self.path = state_dir / "state.json"
        self.date = today_str()
        self.seconds_used_today = 0
        self._load()

    def _load(self):
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                file_date = data.get("date")
                if file_date == today_str():
                    self.date = file_date
                    self.seconds_used_today = int(data.get("seconds_used_today", 0))
                else:
                    # New day
                    self.date = today_str()
                    self.seconds_used_today = 0
            except Exception:
                # Corrupt or unreadable state: reset
                self.date = today_str()
                self.seconds_used_today = 0
        else:
            self._save()

    def _save(self):
        tmp = {
            "date": self.date,
            "seconds_used_today": int(self.seconds_used_today),
        }
        try:
            with self.path.open("w", encoding="utf-8") as f:
                json.dump(tmp, f)
        except Exception:
            # Best-effort; ignore errors
            pass

    def tick(self, elapsed_seconds: int):
        # Handle day roll-over
        current = today_str()
        if current != self.date:
            self.date = current
            self.seconds_used_today = 0
        self.seconds_used_today = max(0, int(self.seconds_used_today) + int(elapsed_seconds))
        self._save()

    def remaining_seconds(self) -> int:
        remaining = DAILY_LIMIT_SECONDS - int(self.seconds_used_today)
        return max(0, remaining)


class SessionLogger:
    def __init__(self, logs_dir: Path):
        self.logs_dir = logs_dir
        self.current_entry = None

    def _log_path(self) -> Path:
        date_str = today_str()
        user_log_dir = self.logs_dir / date_str
        user_log_dir.mkdir(parents=True, exist_ok=True)
        # One file per user per day
        username = get_username()
        return user_log_dir / f"{username}.jsonl"

    def start(self, category: str, plan_text: str):
        now_iso = datetime.now(timezone.utc).isoformat()
        self.current_entry = {
            "start": now_iso,
            "category": category,
            "plan": plan_text,
        }
        # Write start immediately
        self._append_line({"event": "start", **self.current_entry})

    def end(self, duration_seconds: int, reason: str):
        now_iso = datetime.now(timezone.utc).isoformat()
        entry = {
            "event": "end",
            "end": now_iso,
            "duration_seconds": int(duration_seconds),
            "reason": reason,
        }
        if self.current_entry:
            entry.update({
                "category": self.current_entry.get("category"),
                "plan": self.current_entry.get("plan"),
                "start": self.current_entry.get("start"),
            })
        self._append_line(entry)
        self.current_entry = None

    def _append_line(self, obj: dict):
        try:
            with self._log_path().open("a", encoding="utf-8") as f:
                f.write(json.dumps(obj) + "\n")
        except Exception:
            pass


class TimekeeperApp:
    def __init__(self):
        self.username = get_username()
        self.state_dir, self.logs_dir = get_data_dirs()
        self.state = PersistentState(self.state_dir)
        self.logger = SessionLogger(self.logs_dir)
        self.last_tick_monotonic = time.monotonic()
        self.session_started = False
        self.session_start_seconds_used = None

        self.root = tk.Tk()
        self.root.title("Computer Safety")
        # Fullscreen and topmost to strongly encourage compliance
        self.root.attributes("-fullscreen", True)
        try:
            self.root.attributes("-topmost", True)
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self._disable_close)

        self._build_ui()

        # If already out of time, show message and logout
        if self.state.remaining_seconds() <= 0:
            self._show_time_up_and_logout(immediate=True)
        else:
            # Start ticking immediately; time counts from login regardless of goal submission
            self._schedule_tick()

    def _disable_close(self):
        # Ignore attempts to close the window
        pass

    def _build_ui(self):
        container = ttk.Frame(self.root, padding=32)
        container.pack(fill=tk.BOTH, expand=True)

        title = ttk.Label(container, text="Computer Time", font=("Helvetica", 32, "bold"))
        title.pack(pady=(0, 16))

        self.remaining_label = ttk.Label(container, text="", font=("Helvetica", 28))
        self.remaining_label.pack(pady=(0, 24))

        # Goal selection UI
        goal_frame = ttk.Frame(container)
        goal_frame.pack(pady=12)

        goal_title = ttk.Label(goal_frame, text="Set your goal for this session:", font=("Helvetica", 18, "bold"))
        goal_title.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.category_var = tk.StringVar(value="")
        categories = ["Research", "Entertainment", "Games"]
        for idx, cat in enumerate(categories):
            rb = ttk.Radiobutton(goal_frame, text=cat, variable=self.category_var, value=cat, command=self._maybe_enable_start)
            rb.grid(row=1, column=idx, padx=8, sticky="w")

        plan_label = ttk.Label(goal_frame, text="What exactly will you do?", font=("Helvetica", 14))
        plan_label.grid(row=2, column=0, columnspan=3, sticky="w", pady=(12, 4))

        self.plan_var = tk.StringVar(value="")
        plan_entry = ttk.Entry(goal_frame, textvariable=self.plan_var, width=64)
        plan_entry.grid(row=3, column=0, columnspan=3, sticky="we")
        plan_entry.bind("<KeyRelease>", lambda e: self._maybe_enable_start())

        self.start_button = ttk.Button(container, text="Start Session", command=self._on_start_session)
        self.start_button.pack(pady=(16, 8))
        # initially disabled until both fields filled
        try:
            self.start_button.config(state=tk.DISABLED)
        except Exception:
            pass

        # Footer hint
        hint = ttk.Label(container, text="Your daily limit is 30 minutes. Time counts from login until logout.", font=("Helvetica", 12))
        hint.pack(side=tk.BOTTOM, pady=(12, 0))

        # Make sure the window grabs focus
        try:
            self.root.focus_force()
            self.root.grab_set_global()
        except Exception:
            pass

    def _maybe_enable_start(self):
        category_ok = bool(self.category_var.get())
        plan_ok = bool(self.plan_var.get().strip())
        if category_ok and plan_ok:
            try:
                self.start_button.config(state=tk.NORMAL)
            except Exception:
                pass
        else:
            try:
                self.start_button.config(state=tk.DISABLED)
            except Exception:
                pass

    def _on_start_session(self):
        if self.session_started:
            return
        category = self.category_var.get()
        plan_text = self.plan_var.get().strip()
        if not category or not plan_text:
            return
        self.session_started = True
        self.logger.start(category, plan_text)
        # Record baseline used time at session start and reset monotonic baseline
        self.session_start_seconds_used = int(self.state.seconds_used_today)
        self.last_tick_monotonic = time.monotonic()
        # Hide the modal to allow desktop use
        self._hide_modal()

    def _hide_modal(self):
        try:
            self.root.attributes("-topmost", False)
        except Exception:
            pass
        try:
            self.root.attributes("-fullscreen", False)
        except Exception:
            pass
        try:
            self.root.withdraw()
        except Exception:
            pass
        try:
            self.root.quit()
        except Exception:
            pass

    def _schedule_tick(self):
        self._tick()
        self.root.after(1000, self._schedule_tick)

    def _tick(self):
        now = time.monotonic()
        elapsed = max(0, int(now - self.last_tick_monotonic))
        self.last_tick_monotonic = now

        # Update persistence only while a session is active
        if elapsed > 0 and self.session_started:
            self.state.tick(elapsed)

        remaining = self.state.remaining_seconds()
        self._update_remaining_label(remaining)

        if remaining <= 0:
            # End session if started, for logging
            if self.session_started:
                start_used = int(self.session_start_seconds_used or 0)
                duration = int(self.state.seconds_used_today) - start_used
                self.logger.end(duration_seconds=max(0, duration), reason="limit_reached")
            self._show_time_up_and_logout(immediate=False)

    def _update_remaining_label(self, remaining_seconds: int):
        mins = remaining_seconds // 60
        secs = remaining_seconds % 60
        text = f"Time remaining today: {mins:02d}:{secs:02d}"
        try:
            self.remaining_label.config(text=text)
        except Exception:
            pass

    def _show_time_up_and_logout(self, immediate: bool):
        # Bring modal back to front in fullscreen to show message
        try:
            self.root.deiconify()
        except Exception:
            pass
        try:
            self.root.attributes("-fullscreen", True)
            self.root.attributes("-topmost", True)
            self.root.focus_force()
            self.root.lift()
        except Exception:
            pass
        # Replace UI with time-up message
        for child in self.root.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass
        msg = ttk.Label(self.root, text="Time is up for today. See you tomorrow!", font=("Helvetica", 28, "bold"))
        msg.pack(expand=True)
        self.root.update_idletasks()

        delay_ms = 100 if immediate else 4000
        self.root.after(delay_ms, self._logout_user)

    def _logout_user(self):
        user = self.username
        # Try loginctl terminate-user first
        commands = [
            ["loginctl", "terminate-user", user],
            # Fallback: kill all processes of the user (forces session end)
            ["pkill", "-KILL", "-u", user],
        ]
        for cmd in commands:
            try:
                subprocess.run(cmd, check=False)
            except Exception:
                continue
        # As a last resort, exit this process; display manager should handle session end if we were the last process
        try:
            self.root.update()
        except Exception:
            pass
        # Give a brief moment before exit
        time.sleep(1)
        os._exit(0)

    def run(self):
        self.root.mainloop()


def main():
    # Optional: allow an environment variable to bypass (for master/unrestricted accounts if accidentally installed)
    if os.environ.get("COMPUTER_SAFETY_DISABLED") == "1":
        sys.exit(0)

    app = TimekeeperApp()
    app.run()


if __name__ == "__main__":
    main()


