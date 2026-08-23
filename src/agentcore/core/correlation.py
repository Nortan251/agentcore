"""Public correlation contract between conversation turns and autonomous runs (Task 42.4d).

This module defines only the immutable public value object. No planner, tool,
permission or model logic lives here. Correlation IDs are trusted runtime
metadata, never model authority.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunCorrelation:
    """Immutable public correlation between a run and its originating turn."""

    run_id: str
    origin_turn_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "origin_turn_id": self.origin_turn_id,
        }
