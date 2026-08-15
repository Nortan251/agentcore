"""Short, sortable-enough identifiers for tasks, runs and events."""

from __future__ import annotations

import uuid


def new_id(prefix: str = "") -> str:
    raw = uuid.uuid4().hex[:12]
    return f"{prefix}_{raw}" if prefix else raw
