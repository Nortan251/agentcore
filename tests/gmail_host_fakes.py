"""Only synthetic OAuth, terminal and memory-socket boundaries for host tests."""

import io
import re
from contextlib import ExitStack, contextmanager
from pathlib import Path

from agentcore.action_journal import JsonFileActionStore, JsonFileEvidenceStore, JsonFileProtectedActionPayloadStore
from agentcore.approvals import JsonFileApprovalStore
from agentcore.credentials import CredentialRuntime, GoogleAccessProfile
from agentcore.gmail_host.cli import main
from agentcore.gmail_host.composition import GmailHostComposition, GmailHostServices
from agentcore.gmail_host.errors import GmailHostError, HostFailure
from agentcore.gmail_host.storage import GmailHostStorage
from agentcore.integrations.gmail.mutation_http import GmailMutationHttp
from gmail_send_fakes import PROTECTOR_KEY, SCOPES, RecordingLogger
from google_oauth_fakes import TestProtector, bundle


SUBJECT = "AgentCore controlled test - synthetic-unique-marker"
BODY = "Harmless synthetic one-message test."


class FakeOperator:
    def __init__(self, *answers, interactive=True):
        self.answers = list(answers)
        self.private = []
        self.prompts = []
        self.interactive = interactive
        self.on_input = None
        self.last_challenge = None

    def require_interactive(self):
        if not self.interactive:
            raise GmailHostError(HostFailure.PRIVATE_TERMINAL)

    def write_private(self, text):
        self.require_interactive()
        self.private.append(text)

    def read_private(self, prompt, *, maximum):
        self.require_interactive()
        self.prompts.append(prompt)
        if self.on_input:
            self.on_input()
        answer = self.answers.pop(0) if self.answers else ""
        if answer == "CHALLENGE":
            answer = re.findall(r"SEND act_[0-9a-f]{12} [0-9a-f]{16}", "".join(self.private))[-1]
            self.last_challenge = answer
        assert len(answer) <= maximum
        return answer


class HostHarness:
    def __init__(self, directory, *, connected=True):
        self.directory = Path(directory)
        self.connected = connected
        self.logger = RecordingLogger()
        self.last_services = None
        self.last_host = None
        self.last_google = None
        self.opens = 0

    @contextmanager
    def services(self):
        self.opens += 1
        with ExitStack() as stack:
            first = not (self.directory / "gmail-host.json").exists()
            storage = stack.enter_context(GmailHostStorage(self.directory, initialize=first))
            google = bundle(self.directory / "google", protector=TestProtector(PROTECTOR_KEY))
            stack.callback(google.vault.close)
            google.transport.initial["scope"] = SCOPES
            google.transport.refreshed["scope"] = SCOPES
            if self.connected and google.service.restore().credential is None:
                google.service.connect(profile=GoogleAccessProfile.GMAIL_SEND)
            runtime = CredentialRuntime(google.vault)
            stack.callback(runtime.close)
            actions = JsonFileActionStore(self.directory / "actions", clock=google.clock)
            stack.callback(actions.close)
            approvals = JsonFileApprovalStore(self.directory / "approvals")
            stack.callback(approvals.close)
            payloads = stack.enter_context(JsonFileProtectedActionPayloadStore(
                self.directory / "payloads", storage.key_provider,
            ))
            evidence = stack.enter_context(JsonFileEvidenceStore(self.directory / "evidence", storage.key_provider))
            services = GmailHostServices(
                google=google.service, runtime=runtime, storage=storage, payloads=payloads, evidence=evidence,
                actions=actions, approvals=approvals, clock=google.clock,
            )
            self.last_services, self.last_google = services, google
            yield services

    @contextmanager
    def host(self, mode=None, *, operator=None, permissions=None):
        with self.services() as services:
            host = GmailHostComposition(
                services, operator or FakeOperator(), transport=GmailMutationHttp(),
                **({} if mode is None else {"mode": mode}), permissions=permissions, logger=self.logger,
            )
            self.last_host = host
            yield host

    def cli(self, argv, *, operator=None):
        output = io.StringIO()
        code = main(argv, operator=operator or FakeOperator(), services_factory=self.services, output=output)
        return code, output.getvalue()

    def ordinary(self):
        return "\n".join(path.read_text(encoding="utf-8") for folder in ("actions", "approvals")
                         for path in (self.directory / folder).glob("*.json"))
