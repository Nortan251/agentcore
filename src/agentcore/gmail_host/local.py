"""Production Windows dependency lifetime. Opening services enables no tools."""

from contextlib import ExitStack, contextmanager

from agentcore.action_journal import (
    JsonFileActionStore, JsonFileEvidenceStore, JsonFileProtectedActionPayloadStore,
)
from agentcore.approvals import JsonFileApprovalStore
from agentcore.credentials import CredentialRuntime, GoogleConnectionService, ProtectedCredentialVault
from agentcore.gmail_host.composition import GmailHostServices
from agentcore.gmail_host.storage import GmailHostStorage, default_host_directory


@contextmanager
def open_local_services():
    with ExitStack() as stack:
        storage = stack.enter_context(GmailHostStorage(default_host_directory()))
        config = storage.oauth_config()
        vault = stack.enter_context(ProtectedCredentialVault(storage.directory / "google"))
        google = GoogleConnectionService(vault, config)
        runtime = CredentialRuntime(vault)
        stack.callback(runtime.close)
        actions = JsonFileActionStore(storage.directory / "actions")
        stack.callback(actions.close)
        approvals = JsonFileApprovalStore(storage.directory / "approvals")
        stack.callback(approvals.close)
        payloads = stack.enter_context(JsonFileProtectedActionPayloadStore(
            storage.directory / "payloads", storage.key_provider,
        ))
        evidence = stack.enter_context(JsonFileEvidenceStore(storage.directory / "evidence", storage.key_provider))
        yield GmailHostServices(
            google=google, runtime=runtime, storage=storage, actions=actions, approvals=approvals,
            payloads=payloads, evidence=evidence,
        )
