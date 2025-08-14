#!/usr/bin/env python3

import json
import os
import sys
import time
import subprocess
import random
from datetime import datetime, timezone
from pathlib import Path
import tkinter as tk
from tkinter import ttk


def _debug_enabled() -> bool:
    return os.environ.get("COMPUTER_SAFETY_DEBUG", "0") in ("1", "true", "TRUE", "yes", "YES")


def _debug_log(message: str):
    if not _debug_enabled():
        return
    try:
        ts = datetime.now().strftime("%H:%M:%S")
        with open("/tmp/timekeeper-debug.log", "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {message}\n")
    except Exception:
        pass


APP_NAME = "computer-safety"
DAILY_LIMIT_SECONDS = 30 * 60  # 30 minutes
STATE_SAVE_INTERVAL_SECONDS = 10  # batch disk writes to reduce IO


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
    """Minimal persistence: per-user daily usage counter and credit system."""

    def __init__(self, state_dir: Path):
        self.path = state_dir / "state.json"
        self.date = today_str()
        self.seconds_used_today = 0
        self.total_credits = 0
        self.questions_answered_today = 0
        self.last_question_date = today_str()
        self._last_save_monotonic = time.monotonic()
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
                    self.total_credits = int(data.get("total_credits", 0))
                    self.questions_answered_today = int(data.get("questions_answered_today", 0))
                    self.last_question_date = data.get("last_question_date", today_str())
                else:
                    # New day - reset daily counters but keep credits
                    self.date = today_str()
                    self.seconds_used_today = 0
                    self.total_credits = int(data.get("total_credits", 0))  # Credits persist
                    self.questions_answered_today = 0
                    self.last_question_date = today_str()
                    self._save()
            except Exception:
                # Corrupt or unreadable state: reset
                self.date = today_str()
                self.seconds_used_today = 0
                self.total_credits = 0
                self.questions_answered_today = 0
                self.last_question_date = today_str()
                self._save()
        else:
            self._save()

    def _save(self):
        tmp_data = {
            "date": self.date,
            "seconds_used_today": int(self.seconds_used_today),
            "total_credits": int(self.total_credits),
            "questions_answered_today": int(self.questions_answered_today),
            "last_question_date": self.last_question_date,
        }
        try:
            tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(tmp_data, f)
            os.replace(tmp_path, self.path)
            _debug_log(f"Saved state: {tmp_data}")
            self._last_save_monotonic = time.monotonic()
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
        # Batch saves to reduce IO
        now_mono = time.monotonic()
        if now_mono - self._last_save_monotonic >= STATE_SAVE_INTERVAL_SECONDS:
            self._save()

    def force_save(self):
        self._save()

    def remaining_seconds(self) -> int:
        remaining = DAILY_LIMIT_SECONDS - int(self.seconds_used_today)
        return max(0, remaining)

    def add_credits(self, amount: int):
        """Add credits to user's account."""
        self.total_credits = max(0, self.total_credits + amount)
        self._save()

    def can_answer_question(self) -> bool:
        """Check if user can answer more questions today."""
        current = today_str()
        if current != self.last_question_date:
            # New day, reset question counter
            self.questions_answered_today = 0
            self.last_question_date = current
            self._save()
        return self.questions_answered_today < 20

    def record_question_answered(self):
        """Record that a question was answered (regardless of correctness)."""
        current = today_str()
        if current != self.last_question_date:
            # New day, reset question counter
            self.questions_answered_today = 0
            self.last_question_date = current
        self.questions_answered_today += 1
        self._save()

    def get_questions_progress(self) -> tuple[int, int]:
        """Get current question progress (answered, total)."""
        current = today_str()
        if current != self.last_question_date:
            return (0, 20)
        return (self.questions_answered_today, 20)


class QuestionGenerator:
    """Generates random math questions for the credit system."""

    def __init__(self):
        self.random = random.Random()

    def generate_question(self, difficulty: str) -> tuple[str, int, int]:
        """
        Generate a random math question.

        Args:
            difficulty: 'easy', 'medium', or 'hard'

        Returns:
            tuple: (question_text, correct_answer, points_earned)
        """
        if difficulty.lower() == 'easy':
            return self._generate_easy_question()
        elif difficulty.lower() == 'medium':
            return self._generate_medium_question()
        else:
            # Default to easy if invalid difficulty
            return self._generate_easy_question()

    def _generate_easy_question(self) -> tuple[str, int, int]:
        """Generate addition or subtraction question (1-100 range)."""
        op = self.random.choice(['+', '-'])
        if op == '+':
            a = self.random.randint(1, 50)
            b = self.random.randint(1, 50)
            answer = a + b
            question = f"What is {a} + {b}?"
        else:  # subtraction
            a = self.random.randint(1, 100)
            b = self.random.randint(1, a)  # Ensure positive result
            answer = a - b
            question = f"What is {a} - {b}?"

        return question, answer, 1  # 1 point for easy

    def _generate_medium_question(self) -> tuple[str, int, int]:
        """Generate multiplication or division question (1-20 range)."""
        op = self.random.choice(['×', '÷'])
        if op == '×':
            a = self.random.randint(1, 20)
            b = self.random.randint(1, 20)
            answer = a * b
            question = f"What is {a} × {b}?"
        else:  # division
            # Generate numbers that divide evenly
            b = self.random.randint(1, 20)
            a = b * self.random.randint(1, 10)  # Ensure division results in whole number
            answer = a // b
            question = f"What is {a} ÷ {b}?"

        return question, answer, 2  # 2 points for medium


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

    def log_credit_earned(self, difficulty: str, question: str, correct: bool, points: int):
        """Log credit earning activity."""
        now_iso = datetime.now(timezone.utc).isoformat()
        entry = {
            "event": "credit_earned",
            "timestamp": now_iso,
            "difficulty": difficulty,
            "question": question,
            "correct": correct,
            "points": points,
        }
        self._append_line(entry)

    def log_question_limit_reached(self):
        """Log when daily question limit is reached."""
        now_iso = datetime.now(timezone.utc).isoformat()
        entry = {
            "event": "question_limit_reached",
            "timestamp": now_iso,
        }
        self._append_line(entry)

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
        self.question_generator = QuestionGenerator()
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
        self.ui_visible = True

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

        # Credit display in top-left
        credit_frame = ttk.Frame(container)
        credit_frame.pack(anchor="nw", pady=(0, 16))
        
        credit_label = tk.Label(credit_frame, text="Credits:", font=("Helvetica", 20, "bold"))
        credit_label.pack(side=tk.LEFT, padx=(0, 8))
        
        self.credit_display = tk.Label(credit_frame, text="0", font=("Helvetica", 24, "bold"), foreground="green")
        self.credit_display.pack(side=tk.LEFT)
        
        # Update credit display
        self._update_credit_display()

        title = tk.Label(container, text="Computer Time", font=("Helvetica", 32, "bold"))
        title.pack(pady=(0, 16))

        self.remaining_label = tk.Label(container, text="", font=("Helvetica", 28))
        self.remaining_label.pack(pady=(0, 24))

        # Goal selection UI
        goal_frame = ttk.Frame(container)
        goal_frame.pack(pady=12)

        goal_title = tk.Label(goal_frame, text="Set your goal for this session:", font=("Helvetica", 18, "bold"))
        goal_title.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        self.category_var = tk.StringVar(value="")
        categories = ["Research", "Entertainment", "Games"]
        for idx, cat in enumerate(categories):
            rb = ttk.Radiobutton(goal_frame, text=cat, variable=self.category_var, value=cat, command=self._maybe_enable_start)
            rb.grid(row=1, column=idx, padx=8, sticky="w")

        plan_label = tk.Label(goal_frame, text="What exactly will you do?", font=("Helvetica", 14))
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

        # Earn Credit button
        self.earn_credit_button = ttk.Button(container, text="Earn Credits", command=self._show_earn_credit_screen)
        self.earn_credit_button.pack(pady=(8, 16))
        
        # Update earn credit button state
        self._update_earn_credit_button_state()

        # Footer hint
        hint = tk.Label(container, text="Your daily limit is 30 minutes. Time counts from login until logout.", font=("Helvetica", 12))
        hint.pack(side=tk.BOTTOM, pady=(12, 0))

        # Make sure the window grabs focus
        try:
            self.root.focus_force()
            self.root.grab_set_global()
        except Exception:
            pass

    def _update_credit_display(self):
        """Update the credit display with current credits."""
        try:
            self.credit_display.config(text=str(self.state.total_credits))
        except Exception:
            pass

    def _update_earn_credit_button_state(self):
        """Update the earn credit button state based on daily question limit."""
        try:
            if self.state.can_answer_question():
                self.earn_credit_button.config(state=tk.NORMAL, text="Earn Credits")
            else:
                self.earn_credit_button.config(state=tk.DISABLED, text="Daily Limit Reached (20/20)")
        except Exception:
            pass

    def _show_earn_credit_screen(self):
        """Show the earn credit screen with questions."""
        # Check if user can answer more questions
        if not self.state.can_answer_question():
            return
        
        # Clear main UI
        for child in self.root.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass
        
        # Build earn credit UI
        self._build_earn_credit_ui()

    def _build_earn_credit_ui(self):
        """Build the earn credit screen UI."""
        container = ttk.Frame(self.root, padding=32)
        container.pack(fill=tk.BOTH, expand=True)

        # Header with back button and progress
        header_frame = ttk.Frame(container)
        header_frame.pack(fill=tk.X, pady=(0, 24))
        
        back_button = ttk.Button(header_frame, text="← Back to Main", command=self._return_to_main, 
                               font=("Helvetica", 14))
        back_button.pack(side=tk.LEFT)
        
        # Progress display (right side)
        progress_frame = ttk.Frame(header_frame)
        progress_frame.pack(side=tk.RIGHT)
        
        progress_text = tk.Label(progress_frame, text="Questions Today:", font=("Helvetica", 16))
        progress_text.pack(side=tk.LEFT, padx=(0, 8))
        
        answered, total = self.state.get_questions_progress()
        self.progress_label = tk.Label(progress_frame, text=f"{answered}/{total}", 
                                      font=("Helvetica", 18, "bold"), foreground="blue")
        self.progress_label.pack(side=tk.LEFT)

        # Title
        title = tk.Label(container, text="Earn Credits!", font=("Helvetica", 32, "bold"))
        title.pack(pady=(0, 16))

        # Credit display
        credit_frame = ttk.Frame(container)
        credit_frame.pack(pady=(0, 24))
        
        credit_label = tk.Label(credit_frame, text="Your Credits:", font=("Helvetica", 20))
        credit_label.pack(side=tk.LEFT, padx=(0, 8))
        
        self.earn_credit_display = tk.Label(credit_frame, text=str(self.state.total_credits), 
                                           font=("Helvetica", 24, "bold"), foreground="green")
        self.earn_credit_display.pack(side=tk.LEFT)

        # Difficulty selection
        difficulty_frame = ttk.Frame(container)
        difficulty_frame.pack(pady=(0, 24))
        
        difficulty_label = tk.Label(difficulty_frame, text="Choose Difficulty:", font=("Helvetica", 18, "bold"))
        difficulty_label.pack(pady=(0, 12))
        
        self.difficulty_var = tk.StringVar(value="easy")
        difficulties = [("Easy (1 point)", "easy"), ("Medium (2 points)", "medium")]
        
        for text, value in difficulties:
            rb = ttk.Radiobutton(difficulty_frame, text=text, variable=self.difficulty_var, 
                               value=value, font=("Helvetica", 14))
            rb.pack(pady=4)

        # Question display
        self.question_frame = ttk.Frame(container)
        self.question_frame.pack(pady=(0, 24))
        
        self.question_label = tk.Label(self.question_frame, text="", font=("Helvetica", 24, "bold"))
        self.question_label.pack(pady=(0, 16))
        
        # Answer input
        answer_frame = ttk.Frame(self.question_frame)
        answer_frame.pack()
        
        answer_label = tk.Label(answer_frame, text="Your Answer:", font=("Helvetica", 16))
        answer_label.pack(side=tk.LEFT, padx=(0, 8))
        
        self.answer_var = tk.StringVar(value="")
        self.answer_entry = ttk.Entry(answer_frame, textvariable=self.answer_var, width=10)
        self.answer_entry.pack(side=tk.LEFT, padx=(0, 16))
        
        self.submit_button = ttk.Button(answer_frame, text="Submit Answer", 
                                      command=self._submit_answer)
        self.submit_button.pack(side=tk.LEFT)
        
        # Result message
        self.result_label = tk.Label(container, text="", font=("Helvetica", 18))
        self.result_label.pack(pady=(0, 16))
        
        # Next question button (initially hidden)
        self.next_button = ttk.Button(container, text="Next Question", 
                                    command=self._next_question)
        
        # Generate first question
        self._generate_new_question()
        
        # Bind Enter key to submit
        self.answer_entry.bind("<Return>", lambda e: self._submit_answer())

    def _generate_new_question(self):
        """Generate a new question based on selected difficulty."""
        difficulty = self.difficulty_var.get()
        self.current_question, self.current_answer, self.current_points = self.question_generator.generate_question(difficulty)
        
        # Update UI
        self.question_label.config(text=self.current_question)
        self.answer_var.set("")
        self.answer_entry.focus()
        
        # Hide result and next button
        self.result_label.config(text="")
        self.next_button.pack_forget()
        
        # Enable submit button
        self.submit_button.config(state=tk.NORMAL)

    def _submit_answer(self):
        """Submit the user's answer and check if correct."""
        try:
            user_answer = int(self.answer_var.get().strip())
        except ValueError:
            self.result_label.config(text="Please enter a valid number!", foreground="red")
            return
        
        # Disable submit button
        self.submit_button.config(state=tk.DISABLED)
        
        # Check answer
        if user_answer == self.current_answer:
            # Correct answer
            self.state.add_credits(self.current_points)
            self.state.record_question_answered()
            
            # Log the activity
            self.logger.log_credit_earned(
                self.difficulty_var.get(), 
                self.current_question, 
                True, 
                self.current_points
            )
            
            # Update displays
            self._update_credit_display()
            self.earn_credit_display.config(text=str(self.state.total_credits))
            self._update_progress()
            
            # Show success message
            self.result_label.config(text=f"Correct! +{self.current_points} credits earned!", foreground="green")
            
            # Check if daily limit reached
            if not self.state.can_answer_question():
                self._show_daily_limit_reached()
                return
        else:
            # Wrong answer
            self.state.record_question_answered()
            
            # Log the activity
            self.logger.log_credit_earned(
                self.difficulty_var.get(), 
                self.current_question, 
                False, 
                0
            )
            
            # Update progress
            self._update_progress()
            
            # Show failure message
            self.result_label.config(text=f"Wrong! The answer was {self.current_answer}. No credits earned.", foreground="red")
            
            # Check if daily limit reached
            if not self.state.can_answer_question():
                self._show_daily_limit_reached()
                return
        
        # Show next question button
        self.next_button.pack()

    def _next_question(self):
        """Move to the next question."""
        self._generate_new_question()

    def _update_progress(self):
        """Update the progress display."""
        answered, total = self.state.get_questions_progress()
        self.progress_label.config(text=f"{answered}/{total}")
        
        # Update main earn credit button state
        self._update_earn_credit_button_state()

    def _show_daily_limit_reached(self):
        """Show message when daily question limit is reached."""
        # Log the event
        self.logger.log_question_limit_reached()
        
        # Clear question area
        for child in self.question_frame.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass
        
        # Show limit message
        limit_label = ttk.Label(self.question_frame, 
                              text="You've reached your daily limit of 20 questions!\nCome back tomorrow for more!", 
                              font=("Helvetica", 20, "bold"), foreground="orange")
        limit_label.pack(pady=20)
        
        # Hide next button
        self.next_button.pack_forget()

    def _return_to_main(self):
        """Return to the main screen."""
        # Rebuild main UI
        self._build_ui()
        self.ui_visible = True
        
        # Update displays
        self._update_credit_display()
        self._update_earn_credit_button_state()

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
        _debug_log("Session started; enabling tick updates")
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
        self.ui_visible = False

    def _schedule_tick(self):
        self._tick()
        self.root.after(1000, self._schedule_tick)

    def _tick(self):
        now = time.monotonic()
        elapsed_float = now - self.last_tick_monotonic
        elapsed = max(0, int(elapsed_float))
        self.last_tick_monotonic = now

        # Update persistence only while a session is active
        if self.session_started:
            if elapsed > 0:
                _debug_log(f"Tick: +{elapsed}s (elapsed_float={elapsed_float:.3f})")
                self.state.tick(elapsed)
            else:
                _debug_log(f"Tick: +0s (elapsed_float={elapsed_float:.3f})")

        remaining = self.state.remaining_seconds()
        if self.ui_visible:
            self._update_remaining_label(remaining)
            # Also update credit displays and button states
            self._update_credit_display()
            self._update_earn_credit_button_state()

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
        # Ensure final state is flushed before ending the session
        try:
            self.state.force_save()
        except Exception:
            pass
        
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
        
        # Show time up message with credit info
        msg_frame = ttk.Frame(self.root)
        msg_frame.pack(expand=True)
        
        time_msg = tk.Label(msg_frame, text="Time is up for today. See you tomorrow!", font=("Helvetica", 28, "bold"))
        time_msg.pack(pady=(0, 20))
        
        # Show final credit balance
        credit_msg = tk.Label(msg_frame, text=f"Your total credits: {self.state.total_credits}", 
                             font=("Helvetica", 20), foreground="green")
        credit_msg.pack()
        
        self.root.update_idletasks()
        self.ui_visible = True

        delay_ms = 100 if immediate else 4000
        self.root.after(delay_ms, self._logout_user)

    def _logout_user(self):
        # Ensure final state is flushed before ending the session
        try:
            self.state.force_save()
        except Exception:
            pass
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


