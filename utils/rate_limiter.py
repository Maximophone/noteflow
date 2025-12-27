"""Rate limiting utility for API calls and message sending."""

import json
import time
import logging
import random
from datetime import datetime, date, time as dt_time
from pathlib import Path
from typing import Dict, Any, Optional, Union
from config.paths import PATHS
from config.logging_config import setup_logger

logger = setup_logger(__name__)

class RateLimiter:
    def __init__(self, 
                 name: str,
                 min_delay_seconds: float = 2.0,
                 max_delay_seconds: float = 5.0,
                 max_per_day: int = 500,
                 night_mode: bool = True,
                 backoff_factor: float = 2.0,
                 max_backoff_seconds: float = 300.0):
        self.name = name
        self.min_delay = min_delay_seconds
        self.max_delay = max_delay_seconds
        self.max_per_day = max_per_day
        self.night_mode = night_mode
        self.backoff_factor = backoff_factor
        self.max_backoff_seconds = max_backoff_seconds
        
        self.consecutive_failures = 0
        self.current_backoff = min_delay_seconds
        
        self.night_start = dt_time(hour=0, minute=30)
        self.morning_start = dt_time(hour=7, minute=30)
        self.morning_end = dt_time(hour=8, minute=0)
        
        self.rate_limit_dir = PATHS.data / "rate_limits"
        self.rate_limit_dir.mkdir(parents=True, exist_ok=True)
        
        self._init_rate_limiting()
    
    def _init_rate_limiting(self):
        self.rate_limit_file = self.rate_limit_dir / f"{self.name}_rate_limit.json"
        
        self.rate_limit_data = {
            "date": str(date.today()),
            "operations_count": 0,
            "last_operation_time": None
        }
        
        if self.rate_limit_file.exists():
            try:
                with open(self.rate_limit_file, 'r') as f:
                    stored_data = json.load(f)
                
                if stored_data["date"] == str(date.today()):
                    self.rate_limit_data = stored_data
                else:
                    self._save_rate_limit_data()
            except Exception as e:
                logger.error(f"Error loading rate limit data: {e}")
                self._save_rate_limit_data()
        else:
            self._save_rate_limit_data()
    
    def _save_rate_limit_data(self):
        try:
            with open(self.rate_limit_file, 'w') as f:
                json.dump(self.rate_limit_data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving rate limit data: {e}")
    
    def _is_night_time(self) -> bool:
        current_time = datetime.now().time()
        return self.night_start <= current_time < self.morning_start

    def _get_morning_resume_time(self) -> float:
        now = datetime.now()
        current_date = now.date()
        
        if now.time() >= self.night_start:
            resume_date = current_date.replace(day=current_date.day + 1)
        else:
            resume_date = current_date
        
        random_minutes = random.randint(0, 30)
        resume_time = datetime.combine(resume_date, self.morning_start)
        resume_time = resume_time.replace(minute=resume_time.minute + random_minutes)
        
        return (resume_time - now).total_seconds()

    def wait(self) -> bool:
        current_time = time.time()
        
        if self.night_mode and self._is_night_time():
            wait_time = self._get_morning_resume_time()
            logger.info(
                f"Night mode active for {self.name}. "
                f"Pausing operations for {wait_time/3600:.1f} hours."
            )
            time.sleep(wait_time+10)
            logger.info(f"Resuming operations for {self.name}")
            self.rate_limit_data = {
                "date": str(date.today()),
                "operations_count": 0,
                "last_operation_time": None
            }
            self._save_rate_limit_data()
            return self.wait()
        
        if self.rate_limit_data["operations_count"] >= self.max_per_day:
            logger.warning(
                f"Daily limit reached for {self.name}: "
                f"{self.rate_limit_data['operations_count']}/{self.max_per_day} operations"
            )
            return False
        
        base_delay = max(self.min_delay, self.current_backoff)
        
        if self.rate_limit_data["last_operation_time"] is not None:
            time_since_last = current_time - self.rate_limit_data["last_operation_time"]
            if time_since_last < base_delay:
                wait_time = base_delay - time_since_last
                if self.max_delay > base_delay:
                    jitter = random.uniform(0, self.max_delay - base_delay)
                    total_wait = wait_time + jitter
                else:
                    total_wait = wait_time
                
                logger.info(
                    f"Rate limiting for {self.name}: waiting {total_wait:.1f} seconds. "
                    f"Operations today: {self.rate_limit_data['operations_count']}/{self.max_per_day}"
                )
                time.sleep(total_wait)
        
        return True

    def record_success(self):
        self.consecutive_failures = 0
        self.current_backoff = self.min_delay
        
        self.rate_limit_data["last_operation_time"] = time.time()
        self.rate_limit_data["operations_count"] += 1
        self._save_rate_limit_data()

    def record_failure(self):
        self.consecutive_failures += 1
        
        self.current_backoff = min(
            self.current_backoff * self.backoff_factor,
            self.max_backoff_seconds
        )
        
        self.rate_limit_data["last_operation_time"] = time.time()
        logger.warning(
            f"Operation failed for {self.name}. "
            f"Consecutive failures: {self.consecutive_failures}. "
            f"Next backoff delay: {self.current_backoff:.1f} seconds"
        )

class ReactiveRateLimiter:
    """A specialized rate limiter for handling API rate limits reactively."""
    
    def __init__(self, 
                 name: str,
                 initial_backoff_seconds: float = 1.0,
                 backoff_factor: float = 2.0,
                 max_backoff_seconds: float = 600.0,
                 max_retries: int = 10,
                 recovery_factor: float = 2.0,
                 min_backoff_threshold: float = 0.01):
        self.name = name
        self.initial_backoff_seconds = initial_backoff_seconds
        self.backoff_factor = backoff_factor
        self.max_backoff_seconds = max_backoff_seconds
        self.max_retries = max_retries
        self.recovery_factor = recovery_factor
        self.min_backoff_threshold = min_backoff_threshold
        
        self.current_backoff = 0
        self._retry_count = 0
        self._has_had_failures = False
        self._consecutive_successes = 0
        
        self.logger = logging.getLogger(__name__)
        
    def wait(self) -> bool:
        if not self._has_had_failures:
            return True
        
        if self.current_backoff > 0:
            self.logger.info(f"Rate limiting for {self.name}: waiting {self.current_backoff:.1f} seconds")
            time.sleep(self.current_backoff)
        
        return True
        
    def record_success(self):
        self._consecutive_successes += 1
        
        if self._has_had_failures and self.current_backoff > 0:
            self.current_backoff = max(0, self.current_backoff / self.recovery_factor)
            
            if self.current_backoff < self.min_backoff_threshold:
                self.current_backoff = 0
            
            if self.current_backoff > 0:
                self.logger.info(
                    f"Successful call for {self.name}. "
                    f"Reducing backoff delay to {self.current_backoff:.1f} seconds"
                )
            else:
                self.logger.info(f"Backoff fully recovered for {self.name} after {self._consecutive_successes} successful calls")
                
                if self._consecutive_successes >= 3 and self.current_backoff == 0:
                    self._has_had_failures = False
                    self._retry_count = 0
        
    def record_failure(self):
        self._has_had_failures = True
        self._retry_count += 1
        self._consecutive_successes = 0
        
        if self.current_backoff == 0:
            self.current_backoff = self.initial_backoff_seconds
        else:
            self.current_backoff = min(
                self.current_backoff * self.backoff_factor,
                self.max_backoff_seconds
            )
            
        self.logger.warning(
            f"Operation failed for {self.name}. "
            f"Consecutive failures: {self._retry_count}. "
            f"Next backoff delay: {self.current_backoff:.1f} seconds"
        )
        
    def exceeded_max_retries(self) -> bool:
        return self._retry_count >= self.max_retries

    def reset_retries(self):
        self._retry_count = 0
        self._has_had_failures = False
        self.current_backoff = 0
        
    def get_retry_count(self) -> int:
        return self._retry_count
        
    def get_max_retries(self) -> int:
        return self.max_retries
        
    def get_current_backoff(self) -> float:
        return self.current_backoff
        
    def get_status_info(self) -> dict:
        return {
            "retry_count": self._retry_count,
            "max_retries": self.max_retries,
            "current_backoff": self.current_backoff,
            "has_had_failures": self._has_had_failures,
            "consecutive_successes": self._consecutive_successes
        }





