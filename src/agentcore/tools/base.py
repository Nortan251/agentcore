"""Tool abstraction.

A tool is a self-describing, permission-annotated unit of capability. It
declares:

* a unique ``name`` and human ``description`` (for future LLM tool-selection),
* a ``parameters`` schema used for validation before execution,
* the :class:`Capability` set it needs, which the policy layer gates on,
* whether it mutates state (``read_only``).

Implementations subclass :class:`Tool` and implement :meth:`run`. Nothing about
the central agent needs to change to add one — including a future local
computer-control tool, which simply declares ``INPUT_CONTROL``/``APP_CONTROL``
and is registered like any other.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

from agentcore.errors import ToolValidationError
from agentcore.permissions.levels import Capability

if TYPE_CHECKING:
    from agentcore.action_journal.idempotency import ActionExecutionPermit


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ARRAY = "array"
    OBJECT = "object"

    def __str__(self) -> str:
        return self.value


_PYTHON_TYPES: dict[ParamType, tuple[type, ...]] = {
    ParamType.STRING: (str,),
    ParamType.INTEGER: (int,),
    ParamType.NUMBER: (int, float),
    ParamType.BOOLEAN: (bool,),
    ParamType.ARRAY: (list, tuple),
    ParamType.OBJECT: (dict,),
}


@dataclass(frozen=True)
class ToolParameter:
    name: str
    type: ParamType = ParamType.STRING
    description: str = ""
    required: bool = True
    default: Any = None
    choices: tuple[Any, ...] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "type": self.type.value,
            "description": self.description,
            "required": self.required,
        }
        if self.default is not None:
            payload["default"] = self.default
        if self.choices:
            payload["choices"] = list(self.choices)
        return payload


@dataclass(frozen=True)
class ToolSpec:
    """Everything the agent knows about a tool without running it."""

    name: str
    description: str
    parameters: tuple[ToolParameter, ...] = ()
    capabilities: frozenset[Capability] = frozenset()
    read_only: bool = True
    version: str = "1.0.0"
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": [p.to_dict() for p in self.parameters],
            "capabilities": sorted(str(c) for c in self.capabilities),
            "read_only": self.read_only,
            "version": self.version,
            "tags": list(self.tags),
        }


@dataclass
class ToolResult:
    """Uniform return envelope. Tools report failure, they don't raise for it."""

    ok: bool
    output: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def success(cls, output: Any = None, **metadata: Any) -> ToolResult:
        return cls(ok=True, output=output, metadata=metadata)

    @classmethod
    def failure(cls, error: str, **metadata: Any) -> ToolResult:
        return cls(ok=False, error=error, metadata=metadata)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "output": self.output, "error": self.error,
                "metadata": self.metadata}


@dataclass(frozen=True)
class ToolContext:
    """Ambient services handed to a tool at call time.

    Passing these in (rather than letting tools import globals) keeps tools
    unit-testable and lets the runtime swap logger/config per invocation.

    **Frozen.** The security-relevant fields — ``workspace_dir`` and
    ``workspace`` — must not be reassignable, or a tool could widen its own
    sandbox and influence a later invocation. Freezing the whole dataclass was
    verified to break nothing: the executor builds a fresh context per call and
    no code in the project ever assigned to a context attribute.

    ``metadata`` remains a mutable mapping so a tool can still stash
    per-invocation scratch data; it is rebuilt per call and is not
    security-relevant. Freezing the dataclass blocks *rebinding* the field,
    which is what matters.
    """

    config: Mapping[str, Any] = field(default_factory=dict)
    logger: Any = None
    #: Canonical workspace root as a string. Kept for backward compatibility
    #: with existing tools; prefer ``workspace`` for anything path-related.
    workspace_dir: str = "."
    #: The :class:`~agentcore.fs.guard.WorkspaceGuard` for this run, or None
    #: when the context was built outside an agent. Filesystem tools must use
    #: this rather than interpreting ``workspace_dir`` themselves.
    workspace: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    #: Present only at the trusted external-idempotency effect seam. It is
    #: executor-created and never derived from model-visible tool arguments.
    action_execution_permit: ActionExecutionPermit | None = None


class Tool(ABC):
    """Base class for all tools."""

    @property
    @abstractmethod
    def spec(self) -> ToolSpec: ...

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def capabilities(self) -> frozenset[Capability]:
        return self.spec.capabilities

    @abstractmethod
    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        """Execute the tool. Should not raise for expected failures."""

    def validate_args(self, args: Mapping[str, Any]) -> dict[str, Any]:
        """Check args against the spec and apply defaults.

        Raises :class:`ToolValidationError` on any mismatch so a malformed model
        call fails before it reaches side-effecting code.
        """
        spec = self.spec
        declared = {p.name: p for p in spec.parameters}
        unknown = set(args) - set(declared)
        if unknown:
            raise ToolValidationError("unknown arguments", tool=spec.name,
                                      unknown=sorted(unknown),
                                      expected=sorted(declared))
        cleaned: dict[str, Any] = {}
        for name, param in declared.items():
            if name not in args:
                if param.required:
                    raise ToolValidationError("missing required argument",
                                              tool=spec.name, argument=name)
                if param.default is not None:
                    cleaned[name] = param.default
                continue
            value = args[name]
            expected = _PYTHON_TYPES[param.type]
            # bool is a subclass of int; don't let it satisfy numeric params
            is_bad_bool = (isinstance(value, bool)
                           and param.type in {ParamType.INTEGER, ParamType.NUMBER})
            if not isinstance(value, expected) or is_bad_bool:
                raise ToolValidationError(
                    "argument has the wrong type", tool=spec.name, argument=name,
                    expected=param.type.value, got=type(value).__name__)
            if param.choices and value not in param.choices:
                raise ToolValidationError("argument not among allowed choices",
                                          tool=spec.name, argument=name,
                                          choices=list(param.choices))
            cleaned[name] = value
        return cleaned


class FunctionTool(Tool):
    """Adapter turning a plain callable into a :class:`Tool`.

    Useful for small tools and for tests; avoids a class per trivial function.
    """

    def __init__(
        self,
        spec: ToolSpec,
        func: Callable[[Mapping[str, Any], ToolContext], Any],
    ) -> None:
        self._spec = spec
        self._func = func

    @property
    def spec(self) -> ToolSpec:
        return self._spec

    def run(self, args: Mapping[str, Any], context: ToolContext) -> ToolResult:
        output = self._func(args, context)
        if isinstance(output, ToolResult):
            return output
        return ToolResult.success(output)


def tool_spec(
    name: str,
    description: str,
    *,
    parameters: Sequence[ToolParameter] = (),
    capabilities: Sequence[Capability] = (),
    read_only: bool = True,
    version: str = "1.0.0",
    tags: Sequence[str] = (),
) -> ToolSpec:
    """Small helper so subclasses can build a spec without tuple noise."""
    return ToolSpec(
        name=name,
        description=description,
        parameters=tuple(parameters),
        capabilities=frozenset(capabilities),
        read_only=read_only,
        version=version,
        tags=tuple(tags),
    )
