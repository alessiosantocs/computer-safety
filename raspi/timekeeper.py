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
        # Try multiple locations for debug log
        log_locations = [
            "/tmp/timekeeper-debug.log",
            os.path.expanduser("~/timekeeper-debug.log"),
            "/var/tmp/timekeeper-debug.log"
        ]
        
        for log_path in log_locations:
            try:
                with open(log_path, "a", encoding="utf-8") as f:
                    f.write(f"[{ts}] {message}\n")
                break  # Successfully wrote to one location
            except (OSError, IOError):
                continue  # Try next location
    except Exception:
        pass  # Silently fail if all logging attempts fail


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
            # Generate numbers that divide evenly, ensuring b is never 0
            b = self.random.randint(1, 20)  # b is 1-20, never 0
            a = b * self.random.randint(1, 10)  # Ensure division results in whole number
            answer = a // b
            question = f"What is {a} ÷ {b}?"

        return question, answer, 2  # 2 points for medium


class SessionLogger:
    def __init__(self, logs_dir: Path):
        self.logs_dir = logs_dir
        self.current_entry = None

    def _log_path(self, date_str: str = None) -> Path:
        if date_str is None:
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

    def log_credit_spent(self, amount: int, description: str):
        """Log credit spending activity (for future use)."""
        now_iso = datetime.now(timezone.utc).isoformat()
        entry = {
            "event": "credit_spent",
            "timestamp": now_iso,
            "amount": amount,
            "description": description,
        }
        self._append_line(entry)

    def get_credit_transactions(self, days_back: int = 30) -> list[dict]:
        """Get all credit transactions from the last N days."""
        transactions = []
        username = get_username()
        
        # Get dates for the last N days
        from datetime import datetime, timedelta
        current_date = datetime.now().date()
        
        for i in range(days_back):
            check_date = current_date - timedelta(days=i)
            date_str = check_date.strftime("%Y-%m-%d")
            log_path = self._log_path(date_str)
            
            if not log_path.exists():
                continue
                
            try:
                with log_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            # Only include credit-related events
                            if entry.get("event") in ["credit_earned", "credit_spent"]:
                                # Add backward compatibility for entries without timestamps
                                if "timestamp" not in entry or not entry["timestamp"]:
                                    # Use the file's date as fallback timestamp
                                    fallback_timestamp = datetime.combine(check_date, datetime.min.time())
                                    entry["timestamp"] = fallback_timestamp.replace(tzinfo=timezone.utc).isoformat()
                                    entry["_fallback_timestamp"] = True  # Mark as fallback for display
                                
                                transactions.append(entry)
                        except json.JSONDecodeError:
                            # Skip malformed JSON lines
                            continue
                        except Exception:
                            # Skip any other parsing errors
                            continue
            except Exception:
                continue
        
        # Sort by timestamp (newest first), with safe fallback for missing timestamps
        def get_sort_key(transaction):
            timestamp = transaction.get("timestamp", "")
            if not timestamp:
                # If still no timestamp, use a very old date so it appears last
                return "1970-01-01T00:00:00Z"
            return timestamp
        
        transactions.sort(key=get_sort_key, reverse=True)
        return transactions

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

        # Button frame for Earn Credits and Account buttons
        button_frame = ttk.Frame(container)
        button_frame.pack(pady=(8, 16))
        
        # Earn Credit button
        self.earn_credit_button = ttk.Button(button_frame, text="Earn Credits", command=self._show_earn_credit_screen)
        self.earn_credit_button.pack(side=tk.LEFT, padx=(0, 8))
        
        # Account button
        self.account_button = ttk.Button(button_frame, text="Your Account", command=self._show_account_screen)
        self.account_button.pack(side=tk.LEFT, padx=(8, 0))
        
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
        
        # Track starting credits for this session
        self.session_start_credits = self.state.total_credits
        
        # Clear all current UI elements and unbind events
        for child in self.root.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass
        
        # Clear any existing bindings to prevent memory leaks
        try:
            self.root.unbind("<Escape>")
        except Exception:
            pass
        
        # Ensure window is properly configured for the new screen
        try:
            self.root.attributes("-fullscreen", True)
            self.root.attributes("-topmost", True)
            self.root.focus_force()
            self.root.lift()
        except Exception:
            pass
        
        # Build earn credit UI
        self._build_earn_credit_ui()
        
        # Update the window
        self.root.update_idletasks()
        self.ui_visible = False  # We're not on the main screen anymore

    def _build_earn_credit_ui(self):
        """Build the earn credit screen UI."""
        try:
            container = ttk.Frame(self.root, padding=32)
            container.pack(fill=tk.BOTH, expand=True)
            
            # Add a simple test label first to see if anything shows up
            test_label = tk.Label(container, text="Building Earn Credit UI...", font=("Helvetica", 16))
            test_label.pack(pady=20)
            
            # Force update to see the test label
            self.root.update_idletasks()

            # Header with back button and progress
            header_frame = ttk.Frame(container)
            header_frame.pack(fill=tk.X, pady=(0, 24))
            
            # Make back button more prominent and ensure it's always visible
            back_button = ttk.Button(header_frame, text="← Back to Main", command=self._return_to_main)
            back_button.pack(side=tk.LEFT, padx=(0, 20))
            
            # Add a backup back button method in case the main one fails
            def backup_return():
                try:
                    self._return_to_main()
                except Exception:
                    # If the main method fails, force a restart
                    self.root.after(100, self._restart_app)
            
            # Bind the backup method to a different event (right-click)
            back_button.bind("<Button-3>", lambda e: backup_return())
            
            # Add a visual separator
            separator = ttk.Separator(header_frame, orient='vertical')
            separator.pack(side=tk.LEFT, fill=tk.Y, padx=10)
            
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

            # Session progress indicator
            if hasattr(self, 'session_start_credits'):
                earned_this_session = self.state.total_credits - self.session_start_credits
                if earned_this_session > 0:
                    session_progress = tk.Label(credit_frame, text=f" (+{earned_this_session} this session)", 
                                              font=("Helvetica", 16), foreground="blue")
                    session_progress.pack(side=tk.LEFT, padx=(8, 0))

            # Difficulty selection
            difficulty_frame = ttk.Frame(container)
            difficulty_frame.pack(pady=(0, 24))
            
            difficulty_label = tk.Label(difficulty_frame, text="Choose Difficulty:", font=("Helvetica", 18, "bold"))
            difficulty_label.pack(pady=(0, 12))
            
            self.difficulty_var = tk.StringVar(value="easy")
            difficulties = [("Easy (1 point)", "easy"), ("Medium (2 points)", "medium")]
            
            for text, value in difficulties:
                rb = ttk.Radiobutton(difficulty_frame, text=text, variable=self.difficulty_var, 
                                   value=value)
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
            
            # Remove the test label now that everything is built
            test_label.destroy()
            
            # Generate first question
            self._generate_new_question()
            
            # Bind Enter key to submit
            self.answer_entry.bind("<Return>", lambda e: self._submit_answer())
            
            # Bind Escape key to go back
            self.root.bind("<Escape>", lambda e: self._return_to_main())
            
            # Final update
            self.root.update_idletasks()
            
        except Exception as e:
            # If there's an error, show it and go back to main
            error_label = tk.Label(self.root, text=f"Error building UI: {str(e)}", font=("Helvetica", 16), foreground="red")
            error_label.pack(expand=True)
            self.root.after(3000, self._return_to_main)  # Go back after 3 seconds

    def _generate_new_question(self):
        """Generate a new question based on selected difficulty."""
        difficulty = self.difficulty_var.get()
        self.current_question, self.current_answer, self.current_points = self.question_generator.generate_question(difficulty)
        
        # Update UI
        self.question_label.config(text=self.current_question)
        self.answer_var.set("")
        
        # Re-enable answer entry and focus on it
        self.answer_entry.config(state=tk.NORMAL)
        self.answer_entry.focus()
        
        # Hide result and next button
        self.result_label.config(text="")
        self.next_button.pack_forget()
        
        # Enable submit button
        self.submit_button.config(state=tk.NORMAL)
        
        # Rebind Enter key to submit answer for the new question
        self.answer_entry.bind("<Return>", lambda e: self._submit_answer())

    def _submit_answer(self):
        """Submit the user's answer and check if correct."""
        try:
            user_answer = int(self.answer_var.get().strip())
        except ValueError:
            self.result_label.config(text="Please enter a valid number!", foreground="red")
            return
        
        # Disable submit button and answer entry to prevent multiple submissions
        self.submit_button.config(state=tk.DISABLED)
        self.answer_entry.config(state=tk.DISABLED)
        
        # Unbind Enter key to prevent repeated submissions
        self.answer_entry.unbind("<Return>")
        
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

    def _show_account_screen(self):
        """Show the account screen with transaction history."""
        # Clear all current UI elements and unbind events
        for child in self.root.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass
        
        # Clear any existing bindings to prevent memory leaks
        try:
            self.root.unbind("<Escape>")
        except Exception:
            pass
        
        # Ensure window is properly configured for the new screen
        try:
            self.root.attributes("-fullscreen", True)
            self.root.attributes("-topmost", True)
            self.root.focus_force()
            self.root.lift()
        except Exception:
            pass
        
        # Build account UI
        self._build_account_ui()
        
        # Update the window
        self.root.update_idletasks()
        self.ui_visible = False  # We're not on the main screen anymore

    def _build_account_ui(self):
        """Build the account screen UI."""
        try:
            container = ttk.Frame(self.root, padding=32)
            container.pack(fill=tk.BOTH, expand=True)

            # Header with back button
            header_frame = ttk.Frame(container)
            header_frame.pack(fill=tk.X, pady=(0, 24))
            
            # Back button
            back_button = ttk.Button(header_frame, text="← Back to Main", command=self._return_to_main_from_account)
            back_button.pack(side=tk.LEFT, padx=(0, 20))
            
            # Title
            title = tk.Label(container, text="Your Account", font=("Helvetica", 32, "bold"))
            title.pack(pady=(0, 16))

            # Current credit balance
            balance_frame = ttk.Frame(container)
            balance_frame.pack(pady=(0, 24))
            
            balance_label = tk.Label(balance_frame, text="Current Balance:", font=("Helvetica", 20))
            balance_label.pack(side=tk.LEFT, padx=(0, 8))
            
            balance_value = tk.Label(balance_frame, text=f"{self.state.total_credits} credits", 
                                   font=("Helvetica", 24, "bold"), foreground="green")
            balance_value.pack(side=tk.LEFT)

            # Transaction history section
            history_label = tk.Label(container, text="Transaction History (Last 30 Days)", 
                                   font=("Helvetica", 18, "bold"))
            history_label.pack(pady=(0, 8), anchor="w")
            
            # Add helpful note about data compatibility
            note_label = tk.Label(container, 
                                text="Note: Older transactions may show approximate times due to data format changes.",
                                font=("Helvetica", 10), foreground="gray")
            note_label.pack(pady=(0, 8), anchor="w")

            # Create scrollable frame for transactions
            canvas = tk.Canvas(container, height=400)
            scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
            scrollable_frame = ttk.Frame(canvas)

            scrollable_frame.bind(
                "<Configure>",
                lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
            )

            canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
            canvas.configure(yscrollcommand=scrollbar.set)

            canvas.pack(side="left", fill="both", expand=True)
            scrollbar.pack(side="right", fill="y")

            # Get and display transactions
            transactions = self.logger.get_credit_transactions()
            
            if not transactions:
                no_trans_label = tk.Label(scrollable_frame, text="No transactions found.", 
                                        font=("Helvetica", 16), foreground="gray")
                no_trans_label.pack(pady=20)
            else:
                # Headers
                header_frame = ttk.Frame(scrollable_frame)
                header_frame.pack(fill=tk.X, pady=(0, 10))
                
                tk.Label(header_frame, text="Date", font=("Helvetica", 12, "bold"), width=12).pack(side=tk.LEFT)
                tk.Label(header_frame, text="Type", font=("Helvetica", 12, "bold"), width=10).pack(side=tk.LEFT)
                tk.Label(header_frame, text="Amount", font=("Helvetica", 12, "bold"), width=8).pack(side=tk.LEFT)
                tk.Label(header_frame, text="Details", font=("Helvetica", 12, "bold"), width=40).pack(side=tk.LEFT)
                
                # Add separator
                separator = ttk.Separator(scrollable_frame, orient='horizontal')
                separator.pack(fill=tk.X, pady=(0, 10))

                # Transaction rows
                for transaction in transactions:
                    self._add_transaction_row(scrollable_frame, transaction)

            # Bind Escape key to go back
            self.root.bind("<Escape>", lambda e: self._return_to_main_from_account())
            
            # Final update
            self.root.update_idletasks()
            
        except Exception as e:
            # If there's an error, show it and go back to main
            error_label = tk.Label(self.root, text=f"Error building Account UI: {str(e)}", 
                                 font=("Helvetica", 16), foreground="red")
            error_label.pack(expand=True)
            self.root.after(3000, self._return_to_main_from_account)  # Go back after 3 seconds

    def _add_transaction_row(self, parent, transaction):
        """Add a single transaction row to the scrollable frame."""
        row_frame = ttk.Frame(parent)
        row_frame.pack(fill=tk.X, pady=2)
        
        # Parse timestamp with improved error handling
        date_str = "Unknown"
        time_str = ""
        is_fallback = transaction.get("_fallback_timestamp", False)
        
        try:
            from datetime import datetime
            timestamp_str = transaction.get("timestamp", "")
            if timestamp_str:
                # Handle different timestamp formats
                if timestamp_str.endswith("Z"):
                    timestamp_str = timestamp_str.replace("Z", "+00:00")
                elif not timestamp_str.endswith("+00:00") and "+" not in timestamp_str[-6:]:
                    # Add UTC timezone if no timezone info
                    timestamp_str += "+00:00"
                
                timestamp = datetime.fromisoformat(timestamp_str)
                date_str = timestamp.strftime("%m/%d/%Y")
                
                if is_fallback:
                    time_str = "(approx)"  # Indicate this is a fallback date
                else:
                    time_str = timestamp.strftime("%H:%M")
        except Exception as e:
            # If all parsing fails, try to extract date from filename or use unknown
            date_str = "Unknown"
            time_str = "(old data)"
        
        # Date column
        date_label = tk.Label(row_frame, text=f"{date_str}\n{time_str}", 
                            font=("Helvetica", 10), width=12, anchor="w")
        date_label.pack(side=tk.LEFT)
        
        # Type column
        event_type = transaction.get("event", "")
        if event_type == "credit_earned":
            type_text = "Earned"
            type_color = "green"
        elif event_type == "credit_spent":
            type_text = "Spent"
            type_color = "red"
        else:
            type_text = "Unknown"
            type_color = "black"
            
        type_label = tk.Label(row_frame, text=type_text, font=("Helvetica", 10), 
                            width=10, anchor="w", foreground=type_color)
        type_label.pack(side=tk.LEFT)
        
        # Amount column with safe fallbacks
        try:
            if event_type == "credit_earned":
                points = transaction.get('points', 0)
                # Handle case where points might be missing or invalid
                if not isinstance(points, (int, float)) or points is None:
                    points = 0
                amount = f"+{int(points)}"
                amount_color = "green"
            elif event_type == "credit_spent":
                spend_amount = transaction.get('amount', 0)
                if not isinstance(spend_amount, (int, float)) or spend_amount is None:
                    spend_amount = 0
                amount = f"-{int(spend_amount)}"
                amount_color = "red"
            else:
                amount = "0"
                amount_color = "black"
        except Exception:
            amount = "?"
            amount_color = "gray"
            
        amount_label = tk.Label(row_frame, text=amount, font=("Helvetica", 10), 
                              width=8, anchor="w", foreground=amount_color)
        amount_label.pack(side=tk.LEFT)
        
        # Details column with safe fallbacks
        try:
            if event_type == "credit_earned":
                difficulty = transaction.get("difficulty", "unknown").capitalize()
                if difficulty.lower() == "unknown" or not difficulty:
                    difficulty = "Unknown"
                
                correct = transaction.get("correct", None)
                if correct is True:
                    status = "Correct"
                elif correct is False:
                    status = "Wrong"
                else:
                    status = "Unknown"
                
                question = transaction.get("question", "")
                if question and len(question) > 30:
                    question = question[:30] + "..."
                
                if question:
                    details = f"{difficulty} question: {question} - {status}"
                else:
                    details = f"{difficulty} question - {status}"
            elif event_type == "credit_spent":
                details = transaction.get("description", "Credit spent")
                if not details:
                    details = "Credit spent"
            else:
                details = "Unknown transaction"
        except Exception:
            details = "Data parsing error"
            
        details_label = tk.Label(row_frame, text=details, font=("Helvetica", 10), 
                               width=40, anchor="w", wraplength=300)
        details_label.pack(side=tk.LEFT)

    def _return_to_main_from_account(self):
        """Return to main screen from account screen."""
        self._do_return_to_main()

    def _return_to_main(self):
        """Return to the main screen."""
        # Check if user has earned credits in this session
        current_credits = self.state.total_credits
        
        # Show confirmation dialog if they've made progress
        if hasattr(self, 'session_start_credits'):
            if current_credits > self.session_start_credits:
                # They earned credits, show confirmation
                self._show_return_confirmation()
                return
        
        # No credits earned or no session, go back directly
        self._do_return_to_main()
    
    def _show_return_confirmation(self):
        """Show confirmation dialog before returning to main."""
        # Create confirmation dialog
        confirm_window = tk.Toplevel(self.root)
        confirm_window.title("Confirm Return")
        confirm_window.geometry("400x200")
        confirm_window.transient(self.root)
        confirm_window.grab_set()
        
        # Center the dialog
        confirm_window.geometry("+%d+%d" % (self.root.winfo_rootx() + 100, self.root.winfo_rooty() + 100))
        
        # Dialog content
        msg = tk.Label(confirm_window, text="You've earned credits in this session!", 
                      font=("Helvetica", 16, "bold"), foreground="green")
        msg.pack(pady=(20, 10))
        
        sub_msg = tk.Label(confirm_window, text="Are you sure you want to return to the main screen?", 
                          font=("Helvetica", 12))
        sub_msg.pack(pady=(0, 20))
        
        # Buttons
        button_frame = ttk.Frame(confirm_window)
        button_frame.pack(pady=(0, 20))
        
        yes_button = ttk.Button(button_frame, text="Yes, Return", 
                               command=lambda: [confirm_window.destroy(), self._do_return_to_main()])
        yes_button.pack(side=tk.LEFT, padx=10)
        
        no_button = ttk.Button(button_frame, text="No, Stay Here", 
                              command=confirm_window.destroy)
        no_button.pack(side=tk.LEFT, padx=10)
        
        # Focus on the dialog
        confirm_window.focus_force()
    
    def _do_return_to_main(self):
        """Actually perform the return to main screen."""
        try:
            # Clear all current UI elements and unbind events
            for child in self.root.winfo_children():
                try:
                    child.destroy()
                except Exception:
                    pass
            
            # Clear any existing bindings to prevent memory leaks
            try:
                self.root.unbind("<Escape>")
            except Exception:
                pass
            
            # Ensure window is properly configured for main screen
            try:
                self.root.attributes("-fullscreen", True)
                self.root.attributes("-topmost", True)
                self.root.focus_force()
                self.root.lift()
            except Exception:
                pass
            
            # Rebuild main UI
            self._build_ui()
            self.ui_visible = True
            
            # Update all displays and states
            self._update_credit_display()
            self._update_earn_credit_button_state()
            
            # Force update to ensure everything is displayed
            self.root.update_idletasks()
            
            # Log the return to main screen for debugging
            _debug_log("Returned to main screen from earn credit screen")
            
        except Exception as e:
            # If something goes wrong, try to recover
            _debug_log(f"Error returning to main: {str(e)}")
            try:
                # Force a complete rebuild
                for child in self.root.winfo_children():
                    child.destroy()
                self._build_ui()
                self.ui_visible = True
                self.root.update_idletasks()
            except Exception:
                # Last resort - restart the app
                self.root.after(1000, self._restart_app)
    
    def _restart_app(self):
        """Restart the app if recovery fails."""
        try:
            # Properly restart the entire program
            python = sys.executable
            os.execl(python, python, *sys.argv)
        except Exception:
            # If all else fails, exit gracefully
            os._exit(1)

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
            try:
                self._update_remaining_label(remaining)
                # Also update credit displays and button states
                self._update_credit_display()
                self._update_earn_credit_button_state()
            except Exception as e:
                _debug_log(f"Error updating UI in tick: {str(e)}")
                # If UI update fails, mark UI as not visible to prevent further errors
                self.ui_visible = False

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
