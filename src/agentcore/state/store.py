"""State persistence.

:class:`StateStore` is the seam that a future memory subsystem plugs into: it
only needs to satisfy save/load/list/delete. Two implementations ship here —
in-memory (tests, ephemeral runs) and JSON-on-disk (durable checkpoints).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Protocol, runtime_checkable

from agentcore.errors import StateError
from agentcore.observability.events import Events
from agentcore.observability.logging import get_logger
from agentcore.state.models import AgentState


@runtime_checkable
class StateStore(Protocol):
    """Persistence interface for agent run state."""

    def save(self, state: AgentState) -> None: ...

    def load(self, run_id: str) -> AgentState: ...

    def exists(self, run_id: str) -> bool: ...

    def list_runs(self) -> list[str]: ...

    def delete(self, run_id: str) -> None: ...


class InMemoryStateStore:
    """Non-durable store. Default for tests and short-lived runs."""

    def __init__(self) -> None:
        self._data: dict[str, dict] = {}

    def save(self, state: AgentState) -> None:
        # store a serialised copy so later mutation doesn't alter the snapshot
        self._data[state.run_id] = state.to_dict()

    def load(self, run_id: str) -> AgentState:
        try:
            return AgentState.from_dict(self._data[run_id])
        except KeyError as exc:
            raise StateError("no saved state for run", run_id=run_id) from exc

    def exists(self, run_id: str) -> bool:
        return run_id in self._data

    def list_runs(self) -> list[str]:
        return sorted(self._data)

    def delete(self, run_id: str) -> None:
        if self._data.pop(run_id, None) is None:
            raise StateError("no saved state for run", run_id=run_id)


class JsonFileStateStore:
    """Durable store writing one JSON file per run.

    Writes go through a temp file + atomic replace so an interrupted checkpoint
    can never leave a half-written state file behind.
    """

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._log = get_logger("state.store")

    def _path(self, run_id: str) -> Path:
        if not run_id or any(c in run_id for c in "/\\" + os.sep):
            raise StateError("invalid run id", run_id=run_id)
        return self.directory / f"{run_id}.json"

    def save(self, state: AgentState) -> None:
        path = self._path(state.run_id)
        payload = json.dumps(state.to_dict(), indent=2, default=str)
        fd, tmp = tempfile.mkstemp(dir=self.directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        self._log.debug(Events.STATE_CHECKPOINT, run_id=state.run_id,
                        version=state.version, path=str(path))

    def load(self, run_id: str) -> AgentState:
        path = self._path(run_id)
        if not path.exists():
            raise StateError("no saved state for run", run_id=run_id,
                             path=str(path))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StateError("saved state is corrupt", run_id=run_id,
                             reason=str(exc)) from exc
        state = AgentState.from_dict(data)
        self._log.info(Events.STATE_RESUMED, run_id=run_id, version=state.version)
        return state

    def exists(self, run_id: str) -> bool:
        return self._path(run_id).exists()

    def list_runs(self) -> list[str]:
        return sorted(p.stem for p in self.directory.glob("*.json"))

    def delete(self, run_id: str) -> None:
        path = self._path(run_id)
        if not path.exists():
            raise StateError("no saved state for run", run_id=run_id)
        path.unlink()
