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
        self.extra_minutes_purchased_today = 0  # Track purchased extra minutes
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
                    self.extra_minutes_purchased_today = int(data.get("extra_minutes_purchased_today", 0))
                else:
                    # New day - reset daily counters but keep credits
                    self.date = today_str()
                    self.seconds_used_today = 0
                    self.total_credits = int(data.get("total_credits", 0))  # Credits persist
                    self.questions_answered_today = 0
                    self.last_question_date = today_str()
                    self.extra_minutes_purchased_today = 0  # Reset purchased minutes daily
                    self._save()
            except Exception:
                # Corrupt or unreadable state: reset
                self.date = today_str()
                self.seconds_used_today = 0
                self.total_credits = 0
                self.questions_answered_today = 0
                self.last_question_date = today_str()
                self.extra_minutes_purchased_today = 0
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
            "extra_minutes_purchased_today": int(self.extra_minutes_purchased_today),
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
        """Calculate remaining seconds with error handling."""
        try:
            # Validate that extra_minutes_purchased_today is reasonable
            extra_minutes = getattr(self, 'extra_minutes_purchased_today', 0)
            if not isinstance(extra_minutes, (int, float)) or extra_minutes < 0:
                _debug_log(f"Invalid extra_minutes_purchased_today: {extra_minutes}, resetting to 0")
                extra_minutes = 0
                self.extra_minutes_purchased_today = 0
            
            # Cap extra minutes at a reasonable maximum (24 hours = 1440 minutes)
            if extra_minutes > 1440:
                _debug_log(f"Extra minutes too high: {extra_minutes}, capping at 1440")
                extra_minutes = 1440
                self.extra_minutes_purchased_today = 1440
            
            # Base daily limit plus any purchased extra minutes
            try:
                extra_seconds = int(extra_minutes * 60)
                total_limit = DAILY_LIMIT_SECONDS + extra_seconds
                
                # Check for overflow
                if total_limit < DAILY_LIMIT_SECONDS:
                    _debug_log("Integer overflow in total_limit calculation")
                    total_limit = DAILY_LIMIT_SECONDS  # Fall back to base limit
            except (OverflowError, ValueError, TypeError) as e:
                _debug_log(f"Error calculating total limit: {str(e)}")
                total_limit = DAILY_LIMIT_SECONDS  # Fall back to base limit
            
            # Calculate remaining time
            try:
                seconds_used = int(getattr(self, 'seconds_used_today', 0))
                if seconds_used < 0:
                    _debug_log(f"Invalid seconds_used_today: {seconds_used}, resetting to 0")
                    seconds_used = 0
                    self.seconds_used_today = 0
                
                remaining = total_limit - seconds_used
                return max(0, remaining)
            except (ValueError, TypeError, OverflowError) as e:
                _debug_log(f"Error calculating remaining seconds: {str(e)}")
                # Return base remaining time as fallback
                return max(0, DAILY_LIMIT_SECONDS - int(getattr(self, 'seconds_used_today', 0)))
                
        except Exception as e:
            _debug_log(f"Unexpected error in remaining_seconds: {str(e)}")
            # Ultimate fallback - return base daily limit
            return max(0, DAILY_LIMIT_SECONDS)

    def add_credits(self, amount: int):
        """Add credits to user's account."""
        self.total_credits = max(0, self.total_credits + amount)
        self._save()

    def buy_minutes(self, minutes: int) -> bool:
        """
        Buy extra minutes using credits. Each minute costs 5 credits.
        
        Args:
            minutes: Number of minutes to purchase
            
        Returns:
            bool: True if purchase successful, False if not enough credits or invalid input
        """
        try:
            # Input validation
            if not isinstance(minutes, int) or minutes <= 0:
                _debug_log(f"Invalid minutes value: {minutes}")
                return False
            
            # Prevent unreasonably large purchases (max 24 hours = 1440 minutes)
            if minutes > 1440:
                _debug_log(f"Minutes too large: {minutes}")
                return False
            
            # Calculate credits needed with overflow protection
            try:
                credits_needed = minutes * 5
                # Check for integer overflow
                if credits_needed < 0 or credits_needed // 5 != minutes:
                    _debug_log(f"Integer overflow in credits calculation: {minutes} * 5")
                    return False
            except (OverflowError, ArithmeticError):
                _debug_log(f"Arithmetic error calculating credits for {minutes} minutes")
                return False
            
            # Check if user has enough credits
            if self.total_credits < credits_needed:
                _debug_log(f"Insufficient credits: need {credits_needed}, have {self.total_credits}")
                return False
            
            # Perform the transaction
            old_credits = self.total_credits
            old_minutes = self.extra_minutes_purchased_today
            
            self.total_credits -= credits_needed
            self.extra_minutes_purchased_today += minutes
            
            # Ensure values remain non-negative
            self.total_credits = max(0, self.total_credits)
            self.extra_minutes_purchased_today = max(0, self.extra_minutes_purchased_today)
            
            try:
                self._save()
                _debug_log(f"Successfully purchased {minutes} minutes for {credits_needed} credits")
                return True
            except Exception as e:
                # Rollback on save failure
                _debug_log(f"Save failed during purchase, rolling back: {str(e)}")
                self.total_credits = old_credits
                self.extra_minutes_purchased_today = old_minutes
                return False
                
        except Exception as e:
            _debug_log(f"Unexpected error in buy_minutes: {str(e)}")
            return False

    def can_buy_minutes(self, minutes: int) -> bool:
        """Check if user has enough credits to buy specified minutes."""
        try:
            # Input validation
            if not isinstance(minutes, int) or minutes <= 0:
                return False
            
            # Prevent unreasonably large purchases (max 24 hours = 1440 minutes)
            if minutes > 1440:
                return False
            
            # Calculate credits needed with overflow protection
            try:
                credits_needed = minutes * 5
                # Check for integer overflow
                if credits_needed < 0 or credits_needed // 5 != minutes:
                    return False
            except (OverflowError, ArithmeticError):
                return False
            
            return self.total_credits >= credits_needed
        except Exception:
            # On any error, assume user cannot buy
            return False

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

    def log_minutes_purchased(self, minutes: int, credits_spent: int):
        """Log minute purchase transaction."""
        try:
            # Input validation
            if not isinstance(minutes, int) or not isinstance(credits_spent, int):
                _debug_log(f"Invalid input types for logging: minutes={type(minutes)}, credits={type(credits_spent)}")
                return
            
            if minutes <= 0 or credits_spent <= 0:
                _debug_log(f"Invalid values for logging: minutes={minutes}, credits={credits_spent}")
                return
            
            now_iso = datetime.now(timezone.utc).isoformat()
            entry = {
                "event": "credit_spent",
                "timestamp": now_iso,
                "amount": int(credits_spent),
                "description": f"Purchased {int(minutes)} extra minutes",
                "minutes_purchased": int(minutes),
            }
            self._append_line(entry)
        except Exception as e:
            _debug_log(f"Error logging minutes purchase: {str(e)}")
            # Don't raise - logging failure shouldn't break the purchase

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

        # Button frame for Earn Credits, Buy Extra Time, and Account buttons
        button_frame = ttk.Frame(container)
        button_frame.pack(pady=(8, 16))
        
        # Earn Credit button
        self.earn_credit_button = ttk.Button(button_frame, text="Earn Credits", command=self._show_earn_credit_screen)
        self.earn_credit_button.pack(side=tk.LEFT, padx=(0, 8))
        
        # Buy Extra Time button
        self.buy_time_button = ttk.Button(button_frame, text="Buy Extra Time", command=self._show_buy_time_screen)
        self.buy_time_button.pack(side=tk.LEFT, padx=(0, 8))
        
        # Account button
        self.account_button = ttk.Button(button_frame, text="Your Account", command=self._show_account_screen)
        self.account_button.pack(side=tk.LEFT, padx=(8, 0))
        
        # Update button states
        self._update_earn_credit_button_state()
        self._update_buy_time_button_state()

        # Footer hint
        hint = tk.Label(container, text="Your daily limit is 30 minutes. You can buy extra time with credits (5 credits = 1 minute).", font=("Helvetica", 12))
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

    def _update_buy_time_button_state(self):
        """Update the buy extra time button state based on available credits."""
        try:
            # Check if button still exists
            if not hasattr(self, 'buy_time_button'):
                return
            
            # Safely check widget existence
            try:
                if not self.buy_time_button.winfo_exists():
                    return
            except Exception:
                return
            
            # Check if state object exists and is valid
            if not hasattr(self, 'state') or self.state is None:
                _debug_log("State object missing in _update_buy_time_button_state")
                return
            
            # Safely check if user can buy minutes
            can_buy = False
            try:
                can_buy = self.state.can_buy_minutes(1)  # Check if can buy at least 1 minute
            except Exception as e:
                _debug_log(f"Error checking can_buy_minutes: {str(e)}")
                can_buy = False
            
            # Update button state
            try:
                if can_buy:
                    self.buy_time_button.config(state=tk.NORMAL, text="Buy Extra Time")
                else:
                    self.buy_time_button.config(state=tk.DISABLED, text="Not Enough Credits")
            except Exception as e:
                _debug_log(f"Error updating buy time button: {str(e)}")
                
        except Exception as e:
            _debug_log(f"Unexpected error in _update_buy_time_button_state: {str(e)}")
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

    def _show_buy_time_screen(self):
        """Show the buy extra time screen."""
        try:
            # Validate state
            if not hasattr(self, 'state') or self.state is None:
                _debug_log("State object missing in _show_buy_time_screen")
                return
            
            # Check if user has any credits
            try:
                if self.state.total_credits < 5:  # Need at least 5 credits for 1 minute
                    _debug_log("User has insufficient credits for buy time screen")
                    return
            except Exception as e:
                _debug_log(f"Error checking credits: {str(e)}")
                return
            
            # Clear all current UI elements and unbind events
            try:
                for child in self.root.winfo_children():
                    try:
                        child.destroy()
                    except Exception:
                        pass
            except Exception as e:
                _debug_log(f"Error clearing UI elements: {str(e)}")
            
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
            except Exception as e:
                _debug_log(f"Error configuring window: {str(e)}")
            
            # Build buy time UI with error handling
            try:
                self._build_buy_time_ui()
            except Exception as e:
                _debug_log(f"Error building buy time UI: {str(e)}")
                # Fall back to main screen if UI building fails
                try:
                    self._do_return_to_main()
                except Exception:
                    _debug_log("Failed to return to main screen after UI error")
                return
            
            # Update the window
            try:
                self.root.update_idletasks()
                self.ui_visible = False  # We're not on the main screen anymore
            except Exception as e:
                _debug_log(f"Error updating window: {str(e)}")
                
        except Exception as e:
            _debug_log(f"Unexpected error in _show_buy_time_screen: {str(e)}")
            # Try to return to main screen as a safety measure
            try:
                self._do_return_to_main()
            except Exception:
                _debug_log("Failed to return to main screen after unexpected error")

    def _build_buy_time_ui(self):
        """Build the buy extra time screen UI."""
        try:
            container = ttk.Frame(self.root, padding=32)
            container.pack(fill=tk.BOTH, expand=True)

            # Header with back button
            header_frame = ttk.Frame(container)
            header_frame.pack(fill=tk.X, pady=(0, 24))
            
            # Back button
            back_button = ttk.Button(header_frame, text="← Back to Main", command=self._return_to_main_from_buy_time)
            back_button.pack(side=tk.LEFT, padx=(0, 20))
            
            # Title
            title = tk.Label(container, text="Buy Extra Time", font=("Helvetica", 32, "bold"))
            title.pack(pady=(0, 16))

            # Current status display
            status_frame = ttk.Frame(container)
            status_frame.pack(pady=(0, 24))
            
            # Current credits
            credit_label = tk.Label(status_frame, text="Your Credits:", font=("Helvetica", 20))
            credit_label.pack(side=tk.LEFT, padx=(0, 8))
            
            self.buy_time_credit_display = tk.Label(status_frame, text=str(self.state.total_credits), 
                                                   font=("Helvetica", 24, "bold"), foreground="green")
            self.buy_time_credit_display.pack(side=tk.LEFT)

            # Current extra time purchased today
            if self.state.extra_minutes_purchased_today > 0:
                extra_time_label = tk.Label(container, 
                                          text=f"Extra time purchased today: {self.state.extra_minutes_purchased_today} minutes", 
                                          font=("Helvetica", 16), foreground="blue")
                extra_time_label.pack(pady=(0, 16))

            # Pricing info
            pricing_frame = ttk.Frame(container)
            pricing_frame.pack(pady=(0, 24))
            
            pricing_title = tk.Label(pricing_frame, text="Pricing:", font=("Helvetica", 18, "bold"))
            pricing_title.pack(pady=(0, 8))
            
            pricing_info = tk.Label(pricing_frame, text="5 credits = 1 minute of extra computer time", 
                                  font=("Helvetica", 16))
            pricing_info.pack()

            # Purchase options
            purchase_frame = ttk.Frame(container)
            purchase_frame.pack(pady=(0, 24))
            
            purchase_title = tk.Label(purchase_frame, text="How many minutes would you like to buy?", 
                                    font=("Helvetica", 18, "bold"))
            purchase_title.pack(pady=(0, 16))

            # Quick purchase buttons
            button_frame = ttk.Frame(purchase_frame)
            button_frame.pack(pady=(0, 16))
            
            # Calculate max minutes user can afford
            max_minutes = self.state.total_credits // 5
            
            # Common purchase options
            purchase_options = [1, 5, 10, 15, 30]
            for minutes in purchase_options:
                if minutes <= max_minutes:
                    credits_needed = minutes * 5
                    btn_text = f"{minutes} min ({credits_needed} credits)"
                    btn = ttk.Button(button_frame, text=btn_text, 
                                   command=lambda m=minutes: self._purchase_minutes(m))
                    btn.pack(side=tk.LEFT, padx=4)

            # Custom amount entry
            custom_frame = ttk.Frame(purchase_frame)
            custom_frame.pack(pady=(16, 0))
            
            custom_label = tk.Label(custom_frame, text="Or enter custom amount:", font=("Helvetica", 14))
            custom_label.pack(side=tk.LEFT, padx=(0, 8))
            
            self.custom_minutes_var = tk.StringVar(value="")
            self.custom_minutes_entry = ttk.Entry(custom_frame, textvariable=self.custom_minutes_var, width=10)
            self.custom_minutes_entry.pack(side=tk.LEFT, padx=(0, 8))
            
            tk.Label(custom_frame, text="minutes", font=("Helvetica", 14)).pack(side=tk.LEFT, padx=(0, 16))
            
            self.custom_buy_button = ttk.Button(custom_frame, text="Buy Custom Amount", 
                                              command=self._purchase_custom_minutes)
            self.custom_buy_button.pack(side=tk.LEFT)

            # Result message area
            self.buy_result_label = tk.Label(container, text="", font=("Helvetica", 16))
            self.buy_result_label.pack(pady=(16, 0))

            # Bind Enter key to custom purchase
            self.custom_minutes_entry.bind("<Return>", lambda e: self._purchase_custom_minutes())
            
            # Bind Escape key to go back
            self.root.bind("<Escape>", lambda e: self._return_to_main_from_buy_time())
            
            # Final update
            self.root.update_idletasks()
            
        except Exception as e:
            # If there's an error, show it and go back to main
            error_label = tk.Label(self.root, text=f"Error building Buy Time UI: {str(e)}", 
                                 font=("Helvetica", 16), foreground="red")
            error_label.pack(expand=True)
            self.root.after(3000, self._return_to_main_from_buy_time)  # Go back after 3 seconds

    def _purchase_minutes(self, minutes: int):
        """Purchase the specified number of minutes."""
        try:
            # Validate input
            if not isinstance(minutes, int) or minutes <= 0:
                _debug_log(f"Invalid minutes value in _purchase_minutes: {minutes}")
                if hasattr(self, 'buy_result_label') and self.buy_result_label.winfo_exists():
                    self.buy_result_label.config(text="Invalid input!", foreground="red")
                return
            
            # Check if UI elements still exist
            if not hasattr(self, 'buy_result_label') or not self.buy_result_label.winfo_exists():
                _debug_log("UI elements no longer exist during purchase")
                return
            
            # Check if user can afford the purchase
            if not self.state.can_buy_minutes(minutes):
                self.buy_result_label.config(text="Not enough credits!", foreground="red")
                return
            
            # Show confirmation dialog
            self._show_purchase_confirmation(minutes)
            
        except Exception as e:
            _debug_log(f"Error in _purchase_minutes: {str(e)}")
            try:
                if hasattr(self, 'buy_result_label') and self.buy_result_label.winfo_exists():
                    self.buy_result_label.config(text="An error occurred. Please try again.", foreground="red")
            except Exception:
                pass  # If even error display fails, fail silently

    def _purchase_custom_minutes(self):
        """Purchase custom number of minutes from entry field."""
        try:
            # Get and validate input
            input_text = self.custom_minutes_var.get().strip()
            if not input_text:
                self.buy_result_label.config(text="Please enter a number!", foreground="red")
                return
            
            # Check for reasonable length to prevent very large numbers
            if len(input_text) > 10:  # More than 10 digits is unreasonable
                self.buy_result_label.config(text="Number too large!", foreground="red")
                return
            
            try:
                minutes = int(input_text)
            except ValueError:
                self.buy_result_label.config(text="Please enter a valid number!", foreground="red")
                return
            except OverflowError:
                self.buy_result_label.config(text="Number too large!", foreground="red")
                return
            
            if minutes <= 0:
                self.buy_result_label.config(text="Please enter a positive number!", foreground="red")
                return
            
            if minutes > 1440:  # More than 24 hours
                self.buy_result_label.config(text="Maximum 1440 minutes (24 hours) allowed!", foreground="red")
                return
            
            # Check if UI elements still exist before proceeding
            if not hasattr(self, 'buy_result_label') or not self.buy_result_label.winfo_exists():
                _debug_log("UI elements no longer exist during custom purchase")
                return
            
            self._purchase_minutes(minutes)
            
        except Exception as e:
            _debug_log(f"Error in _purchase_custom_minutes: {str(e)}")
            try:
                if hasattr(self, 'buy_result_label') and self.buy_result_label.winfo_exists():
                    self.buy_result_label.config(text="An error occurred. Please try again.", foreground="red")
            except Exception:
                pass  # If even error display fails, fail silently

    def _show_purchase_confirmation(self, minutes: int):
        """Show confirmation dialog for purchase."""
        try:
            # Validate input and state
            if not isinstance(minutes, int) or minutes <= 0:
                _debug_log(f"Invalid minutes in confirmation dialog: {minutes}")
                return
            
            credits_needed = minutes * 5
            
            # Double-check the user still has enough credits
            if not self.state.can_buy_minutes(minutes):
                if hasattr(self, 'buy_result_label') and self.buy_result_label.winfo_exists():
                    self.buy_result_label.config(text="Not enough credits!", foreground="red")
                return
            
            # Create confirmation dialog with error handling
            try:
                confirm_window = tk.Toplevel(self.root)
                confirm_window.title("Confirm Purchase")
                confirm_window.geometry("450x250")
                confirm_window.transient(self.root)
                confirm_window.grab_set()
            except Exception as e:
                _debug_log(f"Failed to create confirmation window: {str(e)}")
                # Fall back to direct purchase without confirmation
                self._complete_purchase(minutes)
                return
            
            try:
                # Center the dialog
                confirm_window.geometry("+%d+%d" % (self.root.winfo_rootx() + 100, self.root.winfo_rooty() + 100))
                
                # Dialog content
                msg = tk.Label(confirm_window, text="Confirm Purchase", 
                              font=("Helvetica", 18, "bold"))
                msg.pack(pady=(20, 10))
                
                details = tk.Label(confirm_window, 
                                  text=f"Buy {minutes} minute{'s' if minutes != 1 else ''} for {credits_needed} credits?", 
                                  font=("Helvetica", 14))
                details.pack(pady=(0, 10))
                
                balance_info = tk.Label(confirm_window, 
                                       text=f"Current balance: {self.state.total_credits} credits\nAfter purchase: {self.state.total_credits - credits_needed} credits", 
                                       font=("Helvetica", 12), foreground="gray")
                balance_info.pack(pady=(0, 20))
                
                # Buttons with error-safe callbacks
                button_frame = ttk.Frame(confirm_window)
                button_frame.pack(pady=(0, 20))
                
                def safe_purchase():
                    try:
                        confirm_window.destroy()
                        self._complete_purchase(minutes)
                    except Exception as e:
                        _debug_log(f"Error in purchase callback: {str(e)}")
                        try:
                            confirm_window.destroy()
                        except Exception:
                            pass
                
                def safe_cancel():
                    try:
                        confirm_window.destroy()
                    except Exception as e:
                        _debug_log(f"Error in cancel callback: {str(e)}")
                
                yes_button = ttk.Button(button_frame, text="Yes, Buy Now", command=safe_purchase)
                yes_button.pack(side=tk.LEFT, padx=10)
                
                no_button = ttk.Button(button_frame, text="Cancel", command=safe_cancel)
                no_button.pack(side=tk.LEFT, padx=10)
                
                # Focus on the dialog
                confirm_window.focus_force()
                
            except Exception as e:
                _debug_log(f"Error building confirmation dialog: {str(e)}")
                try:
                    confirm_window.destroy()
                except Exception:
                    pass
                # Fall back to direct purchase
                self._complete_purchase(minutes)
                
        except Exception as e:
            _debug_log(f"Error in _show_purchase_confirmation: {str(e)}")
            # Try to show error message if possible
            try:
                if hasattr(self, 'buy_result_label') and self.buy_result_label.winfo_exists():
                    self.buy_result_label.config(text="Error showing confirmation. Please try again.", foreground="red")
            except Exception:
                pass

    def _complete_purchase(self, minutes: int):
        """Complete the purchase after confirmation."""
        try:
            # Validate input
            if not isinstance(minutes, int) or minutes <= 0:
                _debug_log(f"Invalid minutes in _complete_purchase: {minutes}")
                if hasattr(self, 'buy_result_label') and self.buy_result_label.winfo_exists():
                    self.buy_result_label.config(text="Invalid purchase request!", foreground="red")
                return
            
            credits_needed = minutes * 5
            
            # Final validation before purchase
            if not self.state.can_buy_minutes(minutes):
                _debug_log(f"Cannot buy {minutes} minutes - insufficient credits")
                if hasattr(self, 'buy_result_label') and self.buy_result_label.winfo_exists():
                    self.buy_result_label.config(text="Not enough credits!", foreground="red")
                return
            
            # Attempt the purchase
            purchase_success = False
            try:
                purchase_success = self.state.buy_minutes(minutes)
            except Exception as e:
                _debug_log(f"Error during state.buy_minutes: {str(e)}")
                purchase_success = False
            
            if purchase_success:
                try:
                    # Log the transaction (non-critical, don't fail purchase if this fails)
                    self.logger.log_minutes_purchased(minutes, credits_needed)
                except Exception as e:
                    _debug_log(f"Failed to log purchase, but purchase succeeded: {str(e)}")
                
                # Update displays with error handling
                try:
                    if hasattr(self, 'buy_time_credit_display') and self.buy_time_credit_display.winfo_exists():
                        self.buy_time_credit_display.config(text=str(self.state.total_credits))
                except Exception as e:
                    _debug_log(f"Failed to update credit display: {str(e)}")
                
                # Show success message
                try:
                    if hasattr(self, 'buy_result_label') and self.buy_result_label.winfo_exists():
                        self.buy_result_label.config(
                            text=f"Success! Purchased {minutes} minute{'s' if minutes != 1 else ''} for {credits_needed} credits.",
                            foreground="green"
                        )
                except Exception as e:
                    _debug_log(f"Failed to show success message: {str(e)}")
                
                # Clear custom entry with error handling
                try:
                    if hasattr(self, 'custom_minutes_var'):
                        self.custom_minutes_var.set("")
                except Exception as e:
                    _debug_log(f"Failed to clear custom entry: {str(e)}")
                
                # Schedule UI refresh with error handling
                try:
                    self.root.after(2000, self._safe_refresh_buy_time_ui)
                except Exception as e:
                    _debug_log(f"Failed to schedule UI refresh: {str(e)}")
                    # Try immediate refresh as fallback
                    try:
                        self._safe_refresh_buy_time_ui()
                    except Exception:
                        _debug_log("Even immediate UI refresh failed")
            else:
                # Purchase failed
                _debug_log(f"Purchase failed for {minutes} minutes")
                try:
                    if hasattr(self, 'buy_result_label') and self.buy_result_label.winfo_exists():
                        self.buy_result_label.config(text="Purchase failed! Please try again.", foreground="red")
                except Exception as e:
                    _debug_log(f"Failed to show error message: {str(e)}")
                    
        except Exception as e:
            _debug_log(f"Unexpected error in _complete_purchase: {str(e)}")
            try:
                if hasattr(self, 'buy_result_label') and self.buy_result_label.winfo_exists():
                    self.buy_result_label.config(text="An error occurred during purchase. Please try again.", foreground="red")
            except Exception:
                pass  # If even error display fails, fail silently

    def _refresh_buy_time_ui(self):
        """Refresh the buy time UI to show updated information."""
        try:
            self._build_buy_time_ui()
        except Exception as e:
            _debug_log(f"Error refreshing buy time UI: {str(e)}")

    def _safe_refresh_buy_time_ui(self):
        """Safely refresh the buy time UI with comprehensive error handling."""
        try:
            # Check if we're still in the buy time screen
            if not hasattr(self, 'buy_time_credit_display'):
                _debug_log("Not in buy time screen, skipping refresh")
                return
            
            # Check if root window still exists
            if not hasattr(self, 'root') or not self.root.winfo_exists():
                _debug_log("Root window no longer exists, cannot refresh")
                return
            
            self._build_buy_time_ui()
            
        except Exception as e:
            _debug_log(f"Error in safe refresh: {str(e)}")
            # If refresh fails, try to at least update the credit display
            try:
                if hasattr(self, 'buy_time_credit_display') and self.buy_time_credit_display.winfo_exists():
                    self.buy_time_credit_display.config(text=str(self.state.total_credits))
            except Exception:
                _debug_log("Even fallback credit display update failed")

    def _return_to_main_from_buy_time(self):
        """Return to main screen from buy time screen."""
        self._do_return_to_main()

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
            self._update_buy_time_button_state()
            
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
                self._update_buy_time_button_state()
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
