"""Explicit journal-bound, create-only directory tool; absent from defaults."""

import hashlib
import json
import os
import threading

from agentcore.action_journal import (
    ActionAdapterSpec, ActionEffectClass, ActionJournalEligibility,
    ActionReconciliationResult, EffectVerdict, NormalizedActionEffect,
    PreparedActionIntent, ReconciliationDisposition,
)
from agentcore.fs.mutation import relative_path
from agentcore.permissions.levels import Capability
from agentcore.tools.base import Tool, ToolParameter, ParamType, ToolResult, tool_spec
from agentcore.tools.builtin._filesystem_execution import _FilesystemMutationExecution

_KIND = "filesystem.create_directory.v1"


class CreateDirectoryTool(Tool):
    def __init__(self, guard):
        self._guard = guard
        self._spec = tool_spec("create_directory",
            "Create one missing workspace directory. Parents must already exist; "
            "existing targets are refused. No recursive creation or replacement.",
            parameters=(ToolParameter("path", ParamType.STRING,
                "Workspace-relative directory target.", max_length=4096),),
            capabilities=(Capability.FS_WRITE,), read_only=False, tags=("filesystem",))

    @property
    def spec(self):
        return self._spec

    def validate_contract(self):
        if self.spec.capabilities != frozenset({Capability.FS_WRITE}) or self.spec.read_only:
            raise ValueError("invalid directory create contract")

    def validate_args(self, args):
        cleaned = super().validate_args(args)
        relative_path(self._guard, cleaned["path"])
        return cleaned

    def run(self, args, context):
        execution = context.filesystem_mutation_execution
        if type(execution) is not _FilesystemMutationExecution or execution.tool is not self or (
                not execution.active or execution.used or execution.pid != os.getpid()
                or execution.thread != threading.get_ident() or context.workspace is not self._guard):
            return ToolResult.failure("Executor journal context required.", kind="directory_context_required")
        execution.used = True
        payload = execution.payload
        if payload["tool"] != self.name or payload["version"] != self.spec.version or (
                payload["schema"] != 1 or payload["mode"] != "create_directory"
                or payload["pre_state"] != "absent" or payload["path"] != relative_path(
                    self._guard, args["path"]).as_posix()):
            raise ValueError("directory intent mismatch")

        def validate():
            execution.validate()
            execution.entered = True

        with execution.authorize():
            effect = self._guard.create_directory(payload, validate)
        return ToolResult.success(effect, directory_effect=effect)


class FilesystemDirectoryFoundation:
    """Exact tool/adapter/reconciler registration, with no permission grant."""

    def __init__(self, guard):
        guard.config.validate()
        self.guard, self.tool = guard, CreateDirectoryTool(guard)
        self.spec = ActionAdapterSpec(self.tool.name, self.tool.spec.version, _KIND,
            ActionJournalEligibility.DURABLE_LOCAL_MUTATION,
            ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE,
            frozenset({Capability.FS_READ}))

    def register(self, registry, adapters):
        registry.register(self.tool)
        adapters.register(self.tool, self, reconciler=self)

    def prepare_intent(self, cleaned, context):
        payload = self.guard.prepare_create(cleaned["path"])
        payload.update({"schema": 1, "tool": self.tool.name, "version": self.tool.spec.version,
                        "mode": "create_directory", "pre_state": "absent"})
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
        target = hashlib.sha256((payload["workspace"] + "\0" + payload["path"]).encode()).hexdigest()
        return PreparedActionIntent(digest, "workspace.directory", target_fingerprint=target,
            approval_description="Create one workspace directory (existing targets refused).",
            requires_protected_payload=True, protected_payload=payload)

    def normalize_effect(self, result):
        effect = result.metadata.get("directory_effect")
        if result.ok and effect in {"applied", "not_applied"}:
            return NormalizedActionEffect(
                EffectVerdict.CONFIRMED_APPLIED if effect == "applied" else EffectVerdict.CONFIRMED_NOT_APPLIED,
                "filesystem.directory_created" if effect == "applied" else "filesystem.exists",
                "Directory created." if effect == "applied" else "Existing destination refused.")
        return NormalizedActionEffect(EffectVerdict.OUTCOME_UNKNOWN, "filesystem.directory_uncertain",
            "Directory create outcome requires reconciliation.",
            reconciliation_disposition=ReconciliationDisposition.PROBE_LATER)

    def probe(self, context):
        # No pre-publication ownership evidence exists for mkdir v1. Neither
        # presence nor absence can prove historical application, so even a
        # filesystem read would add no decision-relevant evidence. The executor
        # still requires current FS_READ and the exact recovery/registry leases.
        return ActionReconciliationResult(EffectVerdict.OUTCOME_UNKNOWN,
            "filesystem.directory_unverified", "Directory creation history requires human confirmation.",
            reconciliation_disposition=ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED)
