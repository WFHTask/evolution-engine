from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional


@dataclass
class HardStopConfig:
    budget_hard_cap_usd: float
    max_consecutive_failures: int
    max_iterations_per_day: int
    on_trigger: str


@dataclass
class HardStopState:
    halted: bool = False
    halt_reason: Optional[str] = None
    cumulative_cost_usd: float = 0.0
    consecutive_failures: int = 0
    today_date: str = date.today().isoformat()
    today_iterations: int = 0


class HardStops:
    def __init__(self, config: HardStopConfig, state_path: Path):
        self.config = config
        self.state_path = state_path
        self.state = self._load_state()

    def _load_state(self) -> HardStopState:
        if not self.state_path.exists():
            return HardStopState()
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return HardStopState(**data)
        except Exception:
            return HardStopState()

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self.state.__dict__, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def reset_daily_if_needed(self) -> None:
        today = date.today().isoformat()
        if self.state.today_date != today:
            self.state.today_date = today
            self.state.today_iterations = 0
            self._save_state()

    def check_or_raise(self) -> None:
        self.reset_daily_if_needed()
        if self.state.halted:
            raise RuntimeError(f"Hard stop already triggered: {self.state.halt_reason}")
        if self.state.cumulative_cost_usd >= self.config.budget_hard_cap_usd:
            self.trigger(f"budget hard cap reached: {self.state.cumulative_cost_usd} >= {self.config.budget_hard_cap_usd}")
        if self.state.consecutive_failures >= self.config.max_consecutive_failures:
            self.trigger(
                f"max consecutive failures reached: {self.state.consecutive_failures} >= {self.config.max_consecutive_failures}"
            )
        if self.state.today_iterations >= self.config.max_iterations_per_day:
            self.trigger(
                f"max iterations per day reached: {self.state.today_iterations} >= {self.config.max_iterations_per_day}"
            )

    def trigger(self, reason: str) -> None:
        self.state.halted = True
        self.state.halt_reason = reason
        self._save_state()
        if self.config.on_trigger == "halt_and_notify":
            print(f"[HARD STOP] {reason}", file=sys.stderr)
            print("[HARD STOP] Human intervention required. Run `evolve reset --config <cfg>` to resume.", file=sys.stderr)
        raise RuntimeError(f"Hard stop triggered: {reason}")

    def record_iteration(self) -> None:
        self.reset_daily_if_needed()
        self.state.today_iterations += 1
        self._save_state()

    def record_cost(self, usd: float) -> None:
        if usd <= 0:
            return
        self.state.cumulative_cost_usd += float(usd)
        self._save_state()

    def record_failure(self) -> None:
        self.state.consecutive_failures += 1
        self._save_state()

    def record_success(self) -> None:
        self.state.consecutive_failures = 0
        self._save_state()

    def reset_halt(self) -> None:
        """Clear halted state and reset consecutive_failures counter.

        Human intervention implies the underlying issue has been reviewed;
        start fresh so a single stale failure count doesn't immediately re-trigger.
        """
        self.state.halted = False
        self.state.halt_reason = None
        self.state.consecutive_failures = 0
        self._save_state()

