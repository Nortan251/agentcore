"""Authenticated inert workflow continuity; ActionJournal alone owns effects.

Reuses the private workspace slot, immutable protected payload store and pinned
atomic-file primitive. Plaintext relies on host ACLs, not encryption. No pruning.
"""

import hashlib
import hmac
import json
import re

from agentcore.approvals import JsonFileApprovalStore, approval_state_mac
from agentcore.credentials._protected_file import ProtectedFile
from .errors import WorkspaceHostError
from .storage import _new_private_directory, _private_lock_file, _validate_private

MAX_WORKFLOWS = 256
STATE_FILE = "workspace-workflows.json"


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False).encode("ascii")


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise WorkspaceHostError("workflow_storage_failed")
        result[key] = value
    return result


class WorkflowStorage:
    def __init__(self, services):
        self.services = services
        self._file = self.approvals = None
        self._poisoned = False
        storage = services.storage
        try:
            directory = storage.directory / "workflows"
            storage._pin_directory(directory, _new_private_directory(directory), private=True)
            entries = storage._entries(directory)
            for entry in entries:
                _validate_private(entry)
                if entry.name not in {".credential-owner", STATE_FILE}:
                    raise WorkspaceHostError("workflow_storage_failed")
            self._key = storage.key_provider.load().key
            self._file = ProtectedFile(directory, record_name=STATE_FILE)
            if self._file.read_current() is None:
                if entries:
                    raise WorkspaceHostError("workflow_storage_failed")
                self.records = {}
                self.save()
            else:
                self.records = self.load()
            approval_dir = storage.directory / "workflow-approvals"
            storage._pin_directory(approval_dir, _new_private_directory(approval_dir), private=True)
            for entry in storage._entries(approval_dir):
                _validate_private(entry)
                if entry.name != ".agentcore-approval-store.lock" and not re.fullmatch(
                        r"apr_[0-9a-f]{12}\.json", entry.name):
                    raise WorkspaceHostError("workflow_storage_failed")
            # The shared store opens its owner lock with ordinary defaults.
            # As for workspace action/payload locks, create it privately first.
            _private_lock_file(approval_dir / ".agentcore-approval-store.lock")
            self.approvals = JsonFileApprovalStore(approval_dir)
            if len(self.approvals.list_requests()) > MAX_WORKFLOWS:
                raise WorkspaceHostError("workflow_capacity_exhausted")
            for request_id in self.approvals.list_requests():
                self.approval(request_id)
            self._audit_plan_links()
        except BaseException:
            self.close()
            raise

    def _mac(self, value):
        return hmac.new(self._key, b"agentcore.workspace-workflow.v1\0" + encoded(value),
                        hashlib.sha256).hexdigest()

    def load(self):
        self.services.storage.check()
        if self._poisoned:
            raise WorkspaceHostError("workflow_storage_failed")
        raw = self._file.read_current()
        if raw is None or len(raw) > 1_048_576:
            raise WorkspaceHostError("workflow_storage_failed")
        value = json.loads(raw, object_pairs_hook=strict_object)
        if type(value) is not dict or set(value) != {"schema", "records", "mac"}:
            raise WorkspaceHostError("workflow_storage_failed")
        mac = value.pop("mac")
        if type(value["schema"]) is not int or value["schema"] != 1 or (
                type(mac) is not str or not hmac.compare_digest(mac, self._mac(value))):
            raise WorkspaceHostError("workflow_storage_failed")
        records = value["records"]
        if type(records) is not dict or len(records) > MAX_WORKFLOWS:
            raise WorkspaceHostError("workflow_storage_failed")
        for key, record in records.items():
            if not re.fullmatch(r"task_[0-9a-f]{12}", key) or type(record) is not dict or set(record) != {
                    "plan_action", "reference", "digest", "run_id", "phase", "dispatched",
                    "approved", "request_id", "verification"}:
                raise WorkspaceHostError("workflow_storage_failed")
            if record["phase"] not in {"accepted", "dispatched", "stopped", "verified"} or (
                    type(record["dispatched"]) is not bool):
                raise WorkspaceHostError("workflow_storage_failed")
        return records

    def save(self):
        self.services.storage.check()
        if self._poisoned:
            raise WorkspaceHostError("workflow_storage_failed")
        value = {"schema": 1, "records": self.records}
        data = encoded({**value, "mac": self._mac(value)})
        if len(data) > 1_048_576 or len(self.records) > MAX_WORKFLOWS:
            raise WorkspaceHostError("workflow_capacity_exhausted")
        try:
            self._file.replace(data)
            if self.load() != self.records:
                raise WorkspaceHostError("workflow_storage_failed")
        except BaseException:
            # Even a failed save may have published. This live host is unusable.
            self._poisoned = True
            raise

    def approval(self, request_id):
        """Historical evidence authentication only; never current permission."""
        request = self.approvals.load(request_id)
        if not hmac.compare_digest(request.state_mac, approval_state_mac(self._key, request)):
            raise WorkspaceHostError("workflow_storage_failed")
        return request

    def _audit_plan_links(self):
        # Include orphaned immutable plans: a lost acceptance checkpoint must
        # not silently make this domain look unused on the next invocation.
        from agentcore.action_journal.protected_storage import _read_bounded_file

        for path in self.services.storage._entries(self.services.storage.directory / "payloads"):
            if not path.name.startswith("pap_"):
                continue
            value = json.loads(_read_bounded_file(path, 1_048_576), object_pairs_hook=strict_object)
            stored = self.services.payloads.load(path.stem, action_id=value["action_id"],
                                                expected_intent_digest=value["intent_digest"])
            if set(stored.payload) == {"schema", "workflow_id", "workspace", "profile", "objective", "steps"}:
                record = self.records.get(stored.payload["workflow_id"])
                if record is None or record["reference"] != stored.reference or (
                        record["digest"] != stored.intent_digest or record["plan_action"] != stored.action_id):
                    raise WorkspaceHostError("workflow_storage_failed")

    def close(self):
        if self.approvals is not None:
            self.approvals.close()
        if self._file is not None:
            self._file.close()
