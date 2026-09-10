from __future__ import annotations

import gc
import os
import time
from dataclasses import dataclass
from typing import Any

import psutil


@dataclass
class ResourceStats:
    """System resource statistics."""

    memory_used_mb: float
    memory_percent: float
    cpu_percent: float
    timestamp: float


class ResourceMonitor:
    """Monitor system resources to prevent exhaustion."""

    def __init__(self, memory_limit_percent: float = 85.0, check_interval: int = 10) -> None:
        self.memory_limit_percent = memory_limit_percent
        self.check_interval = check_interval
        self.last_check = 0.0
        self.process = psutil.Process(os.getpid())
        self.stats_history: list[ResourceStats] = []

    def check_resources(self, force: bool = False) -> ResourceStats:
        """Check current resource usage."""
        now = time.time()
        if not force and (now - self.last_check) < self.check_interval:
            return self.stats_history[-1] if self.stats_history else self._get_stats()

        self.last_check = now
        stats = self._get_stats()
        self.stats_history.append(stats)

        # Keep only last 100 stats
        if len(self.stats_history) > 100:
            self.stats_history = self.stats_history[-100:]

        return stats

    def _get_stats(self) -> ResourceStats:
        """Get current resource statistics."""
        mem_info = self.process.memory_info()
        memory_used_mb = mem_info.rss / 1024 / 1024
        memory_percent = psutil.virtual_memory().percent
        cpu_percent = self.process.cpu_percent(interval=0.1)

        return ResourceStats(
            memory_used_mb=memory_used_mb,
            memory_percent=memory_percent,
            cpu_percent=cpu_percent,
            timestamp=time.time(),
        )

    def should_gc(self) -> bool:
        """Check if garbage collection should be triggered."""
        stats = self.check_resources()
        return stats.memory_percent > self.memory_limit_percent

    def force_gc(self) -> dict[str, Any]:
        """Force garbage collection and return stats."""
        before = self._get_stats()
        gc.collect()
        after = self._get_stats()

        return {
            "before_mb": before.memory_used_mb,
            "after_mb": after.memory_used_mb,
            "freed_mb": before.memory_used_mb - after.memory_used_mb,
            "freed_percent": (before.memory_used_mb - after.memory_used_mb) / before.memory_used_mb * 100,
        }

    def get_summary(self) -> dict[str, Any]:
        """Get resource usage summary."""
        if not self.stats_history:
            return {}

        memory_values = [s.memory_used_mb for s in self.stats_history]
        cpu_values = [s.cpu_percent for s in self.stats_history]

        return {
            "memory_current_mb": self.stats_history[-1].memory_used_mb,
            "memory_peak_mb": max(memory_values),
            "memory_avg_mb": sum(memory_values) / len(memory_values),
            "cpu_avg_percent": sum(cpu_values) / len(cpu_values),
            "samples": len(self.stats_history),
        }


class ProgressTracker:
    """Track progress of long-running operations."""

    def __init__(self, total: int, description: str = "Processing") -> None:
        self.total = total
        self.current = 0
        self.description = description
        self.start_time = time.time()
        self.last_update = 0.0
        self.update_interval = 1.0  # Update every 1 second

    def update(self, increment: int = 1, force: bool = False) -> None:
        """Update progress."""
        self.current += increment
        now = time.time()

        if force or (now - self.last_update) >= self.update_interval:
            self.last_update = now
            self._print_progress()

    def _print_progress(self) -> None:
        """Print progress bar."""
        if self.total == 0:
            return

        percent = (self.current / self.total) * 100
        elapsed = time.time() - self.start_time
        rate = self.current / elapsed if elapsed > 0 else 0
        eta = (self.total - self.current) / rate if rate > 0 else 0

        bar_length = 40
        filled = int(bar_length * self.current / self.total)
        bar = "#" * filled + "-" * (bar_length - filled)

        print(
            f"\r{self.description}: [{bar}] {self.current}/{self.total} "
            f"({percent:.1f}%) | {rate:.1f} files/s | ETA: {eta:.0f}s",
            end="",
            flush=True,
        )

    def finish(self) -> None:
        """Mark progress as complete."""
        self.current = self.total
        self._print_progress()
        print()  # New line

    def get_stats(self) -> dict[str, Any]:
        """Get progress statistics."""
        elapsed = time.time() - self.start_time
        rate = self.current / elapsed if elapsed > 0 else 0

        return {
            "total": self.total,
            "completed": self.current,
            "percent": (self.current / self.total * 100) if self.total > 0 else 0,
            "elapsed_seconds": elapsed,
            "rate_per_second": rate,
        }
