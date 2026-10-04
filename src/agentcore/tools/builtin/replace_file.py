"""Exact journal-bound full-file replacement with ephemeral trusted revision."""

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
from agentcore.fs.replacement import RevisionRejectedError
from agentcore.permissions.levels import Capability
from agentcore.tools.base import Tool, ToolParameter, ParamType, ToolResult, tool_spec
from agentcore.tools.builtin._filesystem_execution import _FilesystemMutationExecution
from agentcore.tools.builtin.read_file_for_update import ReadFileForUpdateTool

_KIND = "filesystem.replace.v1"


class ReplaceFileTool(Tool):
    def __init__(self, guard):
        self._guard = guard
        self._spec = tool_spec("replace_file", "Replace an entire existing UTF-8 workspace file "
            "using the exact current-turn revision from read_file_for_update. Stale revisions refuse.",
            parameters=(ToolParameter("path", ParamType.STRING, "Workspace-relative existing file.", max_length=4096),
                        ToolParameter("revision", ParamType.STRING, "Opaque current trusted revision.",
                                      max_length=68, pattern=r"rev_[0-9a-f]{64}"),
                        ToolParameter("content", ParamType.STRING, "Complete new UTF-8 file content.",
                                      max_length=guard.config.max_write_bytes)),
            capabilities=(Capability.FS_WRITE,), read_only=False, tags=("filesystem",))

    @property
    def spec(self):
        return self._spec

    def validate_contract(self):
        if self.spec.capabilities != frozenset({Capability.FS_WRITE}) or self.spec.read_only:
            raise ValueError("invalid replacement contract")

    def validate_args(self, args):
        cleaned = super().validate_args(args)
        content_bytes(cleaned["content"], self._guard.config.max_write_bytes)
        relative_path(self._guard, cleaned["path"])
        return cleaned

    def run(self, args, context):
        execution = context.filesystem_mutation_execution
        if type(execution) is not _FilesystemMutationExecution or execution.tool is not self or (
                not execution.active or execution.used or execution.pid != os.getpid()
                or execution.thread != threading.get_ident() or context.workspace is not self._guard):
            return ToolResult.failure("Executor journal context required.", kind="replace_context_required")
        execution.used = True
        payload = execution.payload
        validate_payload(self._guard, payload, self.spec)
        if payload["path"] != relative_path(self._guard, args["path"]).as_posix() or (
                payload["content"] != args["content"]):
            raise ValueError("replacement intent mismatch")
        data = content_bytes(payload["content"], self._guard.config.max_write_bytes)

        def publication():
            execution.validate()
            execution.entered = True

        with execution.authorize():
            try:
                effect = self._guard.replace_file(payload, data, execution.validate, publication)
            except RevisionRejectedError:
                if execution.entered:
                    raise
                effect = "stale"
        return ToolResult.success(effect, replace_effect=effect)


def validate_payload(guard, payload, spec):
    expected = {"schema", "tool", "version", "mode", "path", "old", "content", "size", "content_digest"}
    old_fields = {"identity", "size", "metadata", "change", "path", "directories", "workspace", "digest",
                  "revision_schema", "read_tool", "replace_tool", "tool_version"}
    if payload is None or set(payload) != expected or payload["schema"] != 1 or (
            payload["tool"] != spec.name or payload["version"] != spec.version
            or payload["mode"] != "replace" or set(payload["old"]) != old_fields
            or payload["path"] != payload["old"]["path"]
            or payload["old"]["revision_schema"] != 1
            or payload["old"]["read_tool"] != "read_file_for_update"
            or payload["old"]["replace_tool"] != spec.name
            or payload["old"]["tool_version"] != spec.version):
        raise ValueError("invalid replacement intent")
    data = content_bytes(payload["content"], guard.config.max_write_bytes)
    if payload["size"] != len(data) or hashlib.sha256(data).hexdigest() != payload["content_digest"]:
        raise ValueError("invalid replacement bytes")


class FilesystemReplaceFoundation:
    def __init__(self, guard, revisions, *, max_result_chars):
        guard.config.validate()
        # New content 6x, path twice 6x, full Windows SD as <=32 KiB hex,
        # plus <=16 KiB native IDs/schema/authenticated record overhead.
        if 6 * MAX_WRITE_FILE_BYTES + 12 * 4096 + 32768 + 16384 > MAX_PROTECTED_ACTION_PAYLOAD_BYTES:
            raise ValueError("protected storage cannot hold replacement bound")
        self.guard, self.revisions = guard, revisions
        self.tool = ReplaceFileTool(guard)
        self.read_tool = ReadFileForUpdateTool(guard, revisions, max_result_chars=max_result_chars)
        self.spec = ActionAdapterSpec(self.tool.name, self.tool.spec.version, _KIND,
            ActionJournalEligibility.DURABLE_LOCAL_MUTATION,
            ActionEffectClass.LOCALLY_VERIFIABLE_OR_REVERSIBLE,
            frozenset({Capability.FS_READ}))

    def register(self, registry, adapters):
        registry.register(self.read_tool)
        registry.register(self.tool)
        adapters.register(self.tool, self, reconciler=self)

    def prepare_intent(self, cleaned, context):
        old = self.revisions.consume(cleaned["revision"], cleaned["path"])
        self.guard.check_revision_snapshot(old)
        data = content_bytes(cleaned["content"], self.guard.config.max_write_bytes)
        payload = {"schema": 1, "tool": self.tool.name, "version": self.tool.spec.version,
                   "mode": "replace", "path": old["path"], "old": old,
                   "content": cleaned["content"], "size": len(data),
                   "content_digest": hashlib.sha256(data).hexdigest()}
        digest = hashlib.sha256(json.dumps(payload, ensure_ascii=True, sort_keys=True,
                                          separators=(",", ":")).encode()).hexdigest()
        target = hashlib.sha256((old["workspace"] + "\0" + old["path"]).encode()).hexdigest()
        return PreparedActionIntent(digest, "workspace.file", target_fingerprint=target,
            approval_description="Replace one bounded existing UTF-8 file using a current trusted revision.",
            requires_protected_payload=True, protected_payload=payload)

    def normalize_effect(self, result):
        effect = result.metadata.get("replace_effect")
        if result.ok and effect in {"applied", "stale"}:
            return NormalizedActionEffect(
                EffectVerdict.CONFIRMED_APPLIED if effect == "applied" else EffectVerdict.CONFIRMED_NOT_APPLIED,
                "filesystem.replaced" if effect == "applied" else "filesystem.stale_revision",
                "File replaced." if effect == "applied" else "Stale revision refused; read the file again.")
        return NormalizedActionEffect(EffectVerdict.OUTCOME_UNKNOWN, "filesystem.replace_uncertain",
            "Replacement requires reconciliation.",
            reconciliation_disposition=ReconciliationDisposition.PROBE_LATER)

    def probe(self, context):
        try:
            payload = context.protected_payload
            validate_payload(self.guard, payload, self.tool.spec)
            if self.guard.observe_replace(payload):
                return ActionReconciliationResult(EffectVerdict.CONFIRMED_APPLIED,
                    "filesystem.replacement_matches", "Final file matches the intended replacement.")
        except Exception:
            pass
        # OLD, absent, other/unsafe content and unverifiable metadata NEVER
        # prove historical non-application after possible publication.
        return ActionReconciliationResult(EffectVerdict.OUTCOME_UNKNOWN, "filesystem.replace_unverified",
            "Replacement history requires human confirmation.",
            reconciliation_disposition=ReconciliationDisposition.HUMAN_CONFIRMATION_REQUIRED)
