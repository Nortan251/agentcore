"""Foundation classification and absence of any shipping send transport."""

import ast
import importlib
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentcore.action_journal import ActionAdapterRegistry, ActionEffectClass, ActionJournalEligibility
from agentcore.action_journal.errors import ActionAdapterRegistrationError
from agentcore.credentials import CredentialError
from agentcore.integrations.gmail.send import GmailSendFoundation
from agentcore.permissions.levels import Capability
from agentcore.tools.base import FunctionTool, tool_spec
from agentcore.tools.builtin import default_tools
from gmail_send_fakes import Harness, SENDER, FakeGmailMutation


def test_shipping_gmail_transport_is_still_get_only_and_new_modules_have_no_network_client():
    assert "gmail_send_message" not in {tool.name for tool in default_tools()}
    read_http = importlib.import_module("agentcore.integrations.gmail.http")
    tree = ast.parse(Path(read_http.__file__).read_text(encoding="utf-8"))
    methods = [call.args[0].value for call in ast.walk(tree) if isinstance(call, ast.Call)
               and isinstance(call.func, ast.Attribute) and call.func.attr == "request"
               and call.args and isinstance(call.args[0], ast.Constant)]
    assert methods == ["GET"]
    for name in ("agentcore.integrations.gmail.send", "agentcore.integrations.gmail.send_intent",
                 "agentcore.tools.authenticated_action", "agentcore.credentials.action"):
        module = importlib.import_module(name)
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        imports = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        imports.update(node.module.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module)
        assert not imports & {"http", "httpx", "urllib", "requests", "socket", "smtplib", "googleapiclient", "subprocess"}


@pytest.mark.parametrize("test_only,transport", [(False, FakeGmailMutation()), (True, SimpleNamespace(test_only=False)),
                                                (True, SimpleNamespace()), ("yes", FakeGmailMutation())])
def test_transport_injection_requires_explicit_test_only_labels(tmp_path, test_only, transport):
    h = Harness(tmp_path)
    try:
        with pytest.raises(CredentialError):
            GmailSendFoundation(h.payloads, sender=SENDER, transport=transport, test_only=test_only)
    finally:
        h.close()


@pytest.mark.parametrize("change", ["classification", "idempotency", "no_probe_caps", "generic_tool"])
def test_credential_reconciler_cannot_be_registered_under_wrong_type_or_classification(tmp_path, change):
    h = Harness(tmp_path)
    try:
        tool, adapter = h.foundation.tool, h.foundation.adapter
        if change == "classification":
            adapter.spec = replace(adapter.spec, eligibility=ActionJournalEligibility.LONG_RUNNING_OR_AMBIGUOUS_EFFECT)
        elif change == "idempotency":
            adapter.spec = replace(adapter.spec, effect_class=ActionEffectClass.EXTERNALLY_IDEMPOTENT)
        elif change == "no_probe_caps":
            adapter.spec = replace(adapter.spec, reconciliation_capabilities=frozenset())
        else:
            tool = FunctionTool(tool.spec, lambda *_: "not an authenticated action")
        with pytest.raises(ActionAdapterRegistrationError):
            ActionAdapterRegistry().register(tool, adapter, reconciler=h.foundation.reconciler)
        assert not h.fake.sends
    finally:
        h.close()
