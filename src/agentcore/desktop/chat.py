"""Host-owned opt-in to synchronous Notepad approval in ordinary chat."""

from agentcore.conversation.tool_use import ConversationToolRunner, ToolCallOutcome
from agentcore.permissions.levels import Capability
from agentcore.integrations.computer_control import APP_NAMES, OBSERVATION_TEXT, STATUS_TEXT
from agentcore.desktop.service import UNKNOWN_REASONS


FOREGROUND_RESULT_INSTRUCTIONS = (
    "For get_foreground_app, report the returned app label when status is observed. "
    "A successful unknown result with app=null means the foreground check completed "
    "but could not safely identify the app. Do not guess an app name or describe "
    "that successful result as missing access, permission denial or tool unavailability. "
    "Only report an access or availability failure when the tool reports it."
)


_PRE_DISPATCH_REFUSALS = frozenset({
    "permission_denied", "validation", "tool_shape_changed", "tool_registration_changed",
    "action_adapter_missing", "action_adapter_invalid", "action_adapter_version_mismatch",
})


def _desktop_category(outcome):
    if getattr(outcome, "not_invoked", False):
        return "not_launched"
    invocation = outcome.invocation
    category = invocation.result.metadata.get("kind") if invocation is not None else None
    if category in _PRE_DISPATCH_REFUSALS:
        return "not_launched"
    return category if category in STATUS_TEXT and category != "ready" else "launch_unknown"


class ConsoleNotepadApproval:
    def __init__(self):
        self.request_count = 0
        self.decision = None
        self.explicit_denial = False

    def request_many(self, capabilities, description):
        if not frozenset(capabilities) <= {Capability.APP_CONTROL, Capability.PROCESS_SPAWN}:
            return False
        self.request_count += 1
        self.explicit_denial = False
        try:
            answer = input("[Desktop approval] Open Notepad? Type 'open notepad' to allow: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            self.decision = "cancelled"
            return False
        allowed = answer == "open notepad"
        self.decision = "approved" if allowed else "denied"
        self.explicit_denial = answer == "no"
        return allowed

    def request(self, capability, description):
        return self.request_many((capability,), description)


class NotepadConversationRunner(ConversationToolRunner):
    def __init__(self, executor, registry, *, notepad_tool, **kwargs):
        super().__init__(executor, registry, **kwargs)
        self._notepad = notepad_tool
        self._uncertain = False

    def _run_one(self, call, *, session_id):
        if call.name == self._notepad.name and self._uncertain:
            return ToolCallOutcome(
                call=call, status="failed",
                content=self._render_refusal(call, STATUS_TEXT["launch_unknown"]))
        outcome = super()._run_one(call, session_id=session_id)
        if call.name == self._notepad.name and _desktop_category(outcome) == "launch_unknown":
            # Result loss outside the adapter must also block later chat dispatch.
            self._uncertain = True
            outcome.content = self._render_refusal(call, STATUS_TEXT["launch_unknown"])
        return outcome

    def _approval_required(self, tool_name):
        # Only this exact host-bound tool reaches the executor's approval gate.
        # Other conversational tools retain the ordinary escalation behavior.
        if tool_name == self._notepad.name and self.registry.get(tool_name) is self._notepad:
            return None
        return super()._approval_required(tool_name)


def desktop_notices(result):
    notices = []
    for outcome in getattr(result, "tool_outcomes", ()):
        if outcome.call.name == "get_foreground_app":
            invocation = outcome.invocation
            observation = invocation.result.output if invocation is not None else None
            category = invocation.result.metadata.get("kind") if invocation is not None else None
            if (category == "observed" and invocation.result.ok and type(observation) is dict
                    and type(observation.get("app")) is str and observation["app"] in APP_NAMES.values()):
                message = "Observed foreground application: " + observation["app"] + "."
            elif category in OBSERVATION_TEXT:
                message = OBSERVATION_TEXT[category]
            else:
                message = "No foreground application observation was obtained."
            notices.append("[Desktop] " + message)
            continue
        if outcome.call.name != "open_notepad":
            continue
        category = _desktop_category(outcome)
        message = ("Notepad was not launched by this request."
                   if category == "not_launched" else STATUS_TEXT[category])
        notices.append("[Desktop] " + message)
    return notices


def desktop_tool_calls(result):
    """Closed desktop call metadata; invalid arguments and call IDs stay private.

    A missing executor return does not prove that an external effect was absent.
    """
    calls = []
    for outcome in getattr(result, "tool_outcomes", ()):
        name = outcome.call.name
        if type(name) is not str or name not in ("get_foreground_app", "open_notepad"):
            continue
        arguments = outcome.call.arguments
        status = getattr(outcome, "status", None)
        calls.append({"name": name,
                      "arguments": {} if type(arguments) is dict and not arguments else None,
                      "status": status if status in ("ok", "failed", "denied", "approval_required") else "unknown",
                      "executor_returned": outcome.invocation is not None,
                      "executed": bool(getattr(outcome, "executed", False))})
    return calls


def notepad_evidence(result, approval):
    """Closed operator evidence; gate input, call IDs and arguments stay private."""
    launches = []
    for outcome in result.tool_outcomes:
        if outcome.call.name != "open_notepad":
            continue
        invocation = outcome.invocation
        kind = invocation.result.metadata.get("kind") if invocation is not None else None
        native = kind if type(kind) is str and kind in STATUS_TEXT and kind != "ready" else None
        assessment = _desktop_category(outcome)
        launches.append({
            "arguments_validated": (type(outcome.call.arguments) is dict and not outcome.call.arguments
                                    and (kind == "permission_denied" or native is not None)),
            "executor_category": kind if type(kind) is str and kind in _PRE_DISPATCH_REFUSALS | STATUS_TEXT.keys() else None,
            "launch_assessment": assessment,
            "native_action_invoked": (False if assessment == "not_launched" else
                                      True if native == "launch_accepted" else None),
            "native_outcome": native,
        })
    return {
        "conversation_ok": bool(result.ok),
        "model_reply": result.reply.content if result.reply is not None else None,
        "error_category": getattr(result, "error_category", None),
        "desktop_tool_calls": desktop_tool_calls(result),
        "approval": {"request_count": approval.request_count, "decision": approval.decision,
                     "explicit_denial": approval.explicit_denial},
        "launch_results": launches,
    }


def desktop_observations(result):
    """Tool evidence for JSON callers, separate from generated model prose."""
    observations = []
    for outcome in getattr(result, "tool_outcomes", ()):
        if outcome.call.name != "get_foreground_app":
            continue
        invocation = outcome.invocation
        category = invocation.result.metadata.get("kind") if invocation is not None else None
        output = invocation.result.output if invocation is not None else None
        if (category == "observed" and invocation.result.ok and type(output) is dict
                and type(output.get("app")) is str and output["app"] in APP_NAMES.values()):
            observations.append({"status": "observed", "app": output["app"]})
        else:
            observations.append({"status": category if category in OBSERVATION_TEXT else "not_observed", "app": None})
            reason = invocation.result.metadata.get("observation_reason") if invocation is not None else None
            if category == "unknown" and type(reason) is str and reason in UNKNOWN_REASONS:
                observations[-1]["reason"] = reason
    return observations
