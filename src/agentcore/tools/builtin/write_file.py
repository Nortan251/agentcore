"""Explicit host-enabled create-only tool and its trusted journal composition.

Not part of default_tools(). Direct run calls lack the one-use executor context
and fail closed. Protected intent holds the single content copy needed for exact
execution/recovery; public records contain authenticated digests only.
"""

import hashlib
import json
import os
import threading

from agentcore.action_journal import (
    ActionAdapterSpec, ActionEffectClass, ActionJournalEligibility,
    ActionReconciliationResult, EffectVerdict, NormalizedActionEffect,
    PreparedActionIntent, ReconciliationDisposition,
)
from agentcore.action_journal.protected_storage import MAX_PROTECTED_ACTION_PAYLOAD_BYTES
from agentcore.fs.config import MAX_WRITE_FILE_BYTES
from agentcore.fs.mutation import content_bytes, relative_path
from agentcore.permissions.levels import Capability
from agentcore.tools.base import Tool, ToolParameter, ParamType, ToolResult, tool_spec


_ISSUER = object()
_KIND = "filesystem.create.v1"


class _WriteExecution:
    """One-use, thread/process-local executor context; never serialized authority."""

    def __init__(self, issuer, tool, payload, authorize, validate):
        if issuer is not _ISSUER:
            raise ValueError("executor write context required")
        self.tool, self.payload = tool, payload
        self.authorize, self.validate = authorize, validate
        self.pid, self.thread = os.getpid(), threading.get_ident()
        self.used, self.active, self.entered = False, True, False

    def __repr__(self):
        return "<executor filesystem write context>"

    def __reduce__(self):
        raise TypeError("write context cannot be serialized")


class WriteFileTool(Tool):
    def __init__(self, guard):
        self._guard = guard
        self._spec = tool_spec("write_file", "Create one new bounded UTF-8 file in the workspace. "
            "Existing destinations are always refused; parents must already exist. No overwrite or append.",
            parameters=(ToolParameter("path", ParamType.STRING, "Workspace-relative target.", max_length=4096),
                        ToolParameter("content", ParamType.STRING, "Exact UTF-8 text to create.",
                                      max_length=guard.config.max_write_bytes)),
            capabilities=(Capability.FS_WRITE,), read_only=False, tags=("filesystem",))

    @property
    def spec(self):
        return self._spec

    def validate_contract(self):
        if self.spec.capabilities != frozenset({Capability.FS_WRITE}) or self.spec.read_only:
            raise ValueError("invalid filesystem write contract")

    def validate_args(self, args):
        cleaned = super().validate_args(args)
        # Strict encoding validation occurs before journal/approval/mutation.
        content_bytes(cleaned["content"], self._guard.config.max_write_bytes)
        relative_path(self._guard, cleaned["path"])
        return cleaned

    def run(self, args, context):
        execution = context.filesystem_write_execution
        if type(execution) is not _WriteExecution or execution.tool is not self or (
                not execution.active or execution.used or execution.pid != os.getpid()
                or execution.thread != threading.get_ident() or context.workspace is not self._guard):
            return ToolResult.failure("Executor journal context required.", kind="write_context_required")
        execution.used = True
        payload = execution.payload
        if payload["content"] != args["content"] or payload["path"] != relative_path(
                self._guard, args["path"]).as_posix():
            raise ValueError("write intent mismatch")
        data = content_bytes(payload["content"], self._guard.config.max_write_bytes)
        if hashlib.sha256(data).hexdigest() != payload["content_digest"]:
            raise ValueError("write digest mismatch")
        def validate():
            execution.validate()
            # Called by the guard immediately before exclusive staging create,
            # and again at publication. A denied pre-staging check stays inert.
            execution.entered = True
        with execution.authorize():
            effect = self._guard.create_file(payload, data, validate)
        return ToolResult.success(effect, write_effect=effect)


class FilesystemWriteFoundation:
    """Host owns the guard, journal stores, policy and explicit registration.

    register(registry, action_adapters) binds the exact tool and reconciler.
    Pass both plus the guard and protected payload store to ToolExecutor.
    This composition supplies no permission grant.
    """

    def __init__(self, guard):
        guard.config.validate()
        # Conservative derivation: worst-case JSON escaping is 6x; a bounded
        # path costs <=24 KiB. 8 KiB additionally covers native directory IDs,
        # digest/tool fields and the authenticated storage record envelope.
        if 6 * MAX_WRITE_FILE_BYTES + 6 * 4096 + 8192 > MAX_PROTECTED_ACTION_PAYLOAD_BYTES:
            raise ValueError("protected storage cannot hold the advertised write bound")
        self.guard, self.tool = guard, WriteFileTool(guard)
        self.spec = ActionAdapterSpec("write_file", self.tool.spec.version, _KIND,
            ActionJournalEligibility.DURABLE_LOCAL_MUTATION,
            ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE,
            frozenset({Capability.FS_READ}))

    def register(self, registry, adapters):
        registry.register(self.tool)
        adapters.register(self.tool, self, reconciler=self)

    def prepare_intent(self, cleaned, context):
        data = content_bytes(cleaned["content"], self.guard.config.max_write_bytes)
        payload = self.guard.prepare_create(cleaned["path"])
        payload.update({"schema": 1, "tool": self.tool.spec.name, "version": self.tool.spec.version,
                        "mode": "create", "pre_state": "absent", "content": cleaned["content"],
                        "content_digest": hashlib.sha256(data).hexdigest(), "size": len(data)})
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
        target = hashlib.sha256((payload["workspace"] + "\0" + payload["path"]).encode()).hexdigest()
        return PreparedActionIntent(digest, "workspace.file", target_fingerprint=target,
            approval_description="Create one bounded UTF-8 workspace file (existing targets refused).",
            requires_protected_payload=True, protected_payload=payload)

    def normalize_effect(self, result):
        effect = result.metadata.get("write_effect")
        if result.ok and effect in {"applied", "not_applied"}:
            return NormalizedActionEffect(
                EffectVerdict.CONFIRMED_APPLIED if effect == "applied" else EffectVerdict.CONFIRMED_NOT_APPLIED,
                "filesystem.created" if effect == "applied" else "filesystem.exists",
                "File created." if effect == "applied" else "Existing destination refused.")
        return NormalizedActionEffect(EffectVerdict.OUTCOME_UNKNOWN, "filesystem.uncertain",
            "Filesystem create outcome requires reconciliation.",
            reconciliation_disposition=ReconciliationDisposition.PROBE_LATER)

    def probe(self, context):
        try:
            payload = context.protected_payload
            expected = {"path", "directories", "workspace", "schema", "tool", "version", "mode",
                        "pre_state", "content", "content_digest", "size"}
            if payload is None or set(payload) != expected or payload["schema"] != 1 or (
                    payload["tool"] != self.tool.name or payload["version"] != self.tool.spec.version
                    or payload["mode"] != "create" or payload["pre_state"] != "absent"):
                raise ValueError("invalid write intent")
            data = content_bytes(payload["content"], self.guard.config.max_write_bytes)
            if payload["size"] != len(data) or hashlib.sha256(data).hexdigest() != payload["content_digest"]:
                raise ValueError("invalid write content")
            observed = self.guard.observe_create(payload, len(data))
            if observed is None:
                # Absence after an uncertain attempt is not historical proof:
                # a cooperating process may have removed a published file.
                return ActionReconciliationResult(EffectVerdict.OUTCOME_UNKNOWN,
                    "filesystem.absent_unverified", "Final target is absent; prior publication cannot be excluded.",
                    reconciliation_disposition=ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED)
            if observed == payload["content_digest"]:
                return ActionReconciliationResult(EffectVerdict.CONFIRMED_APPLIED,
                    "filesystem.matches", "Final file matches the intended content.")
        except Exception:
            pass
        return ActionReconciliationResult(EffectVerdict.OUTCOME_UNKNOWN, "filesystem.unverified",
            "Final target cannot be verified; human confirmation required.",
            reconciliation_disposition=ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED)
