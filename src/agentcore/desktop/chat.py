"""Host-owned opt-in to synchronous Notepad approval in ordinary chat."""

from agentcore.conversation.tool_use import ConversationToolRunner
from agentcore.permissions.levels import Capability
from agentcore.integrations.computer_control import STATUS_TEXT


class ConsoleNotepadApproval:
    def request_many(self, capabilities, description):
        if not frozenset(capabilities) <= {Capability.APP_CONTROL, Capability.PROCESS_SPAWN}:
            return False
        try:
            return input("[Desktop approval] Open Notepad? Type 'open notepad' to allow: ").strip().lower() == "open notepad"
        except (EOFError, KeyboardInterrupt):
            return False

    def request(self, capability, description):
        return self.request_many((capability,), description)


class NotepadConversationRunner(ConversationToolRunner):
    def __init__(self, executor, registry, *, notepad_tool, **kwargs):
        super().__init__(executor, registry, **kwargs)
        self._notepad = notepad_tool

    def _approval_required(self, tool_name):
        # Only this exact host-bound tool reaches the executor's approval gate.
        # Other conversational tools retain the ordinary escalation behavior.
        if tool_name == self._notepad.name and self.registry.get(tool_name) is self._notepad:
            return None
        return super()._approval_required(tool_name)


def desktop_notices(result):
    notices = []
    for outcome in getattr(result, "tool_outcomes", ()):
        if outcome.call.name != "open_notepad":
            continue
        invocation = outcome.invocation
        category = invocation.result.metadata.get("kind") if invocation is not None else None
        message = ("Notepad was not launched by this request."
                   if invocation is None or category == "permission_denied"
                   else STATUS_TEXT.get(category, STATUS_TEXT["launch_unknown"]))
        notices.append("[Desktop] " + message)
    return notices
