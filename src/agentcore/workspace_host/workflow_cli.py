"""Private interactive review surface and categorical public workflow result."""

import json
import secrets
import sys

from agentcore.approvals import HumanApprovalDecision
from agentcore.config.loader import load_config
from agentcore.models.factory import provider_from_config
from agentcore.permissions.policy import PermissionPolicy
from .errors import WorkspaceHostError
from .workflow import SupervisedWorkspaceWorkflow, WorkflowProfile, require_supervised_authority


class TerminalWorkflowHuman:
    def _confirm(self, label, value):
        # No piped/transcript/model response can act as a human terminal.
        if not sys.stdin.isatty() or not sys.stderr.isatty():
            raise WorkspaceHostError("workflow_interactive_terminal_required")
        challenge = label + "-" + secrets.token_hex(4)
        # ASCII JSON escapes controls and displays the entire bounded value.
        print(json.dumps(value, ensure_ascii=True, indent=2), file=sys.stderr)
        print("Type " + challenge + " to continue; any other response declines.", file=sys.stderr)
        try:
            answer = input()
        except (EOFError, KeyboardInterrupt):
            return False
        return type(answer) is str and secrets.compare_digest(answer, challenge)

    def accept_plan(self, review):
        return self._confirm("ACCEPT-PLAN", review)

    def approve_create(self, preview, prompt):
        value = {"request_id": prompt.request_id, "create_only": True, **preview}
        return HumanApprovalDecision.APPROVE if self._confirm("APPROVE-CREATE", value) else HumanApprovalDecision.DENY

    def accept_verification(self, review):
        return self._confirm("VERIFY-ONLY", review)


def command_workflow(args):
    result = {"ok": False, "status": "workflow_not_started", "action": None, "verification": None}
    host = None
    try:
        if args.resume is None and args.enable_write is not True:
            raise WorkspaceHostError("workspace_write_opt_in_required")
        config = load_config(args.config)
        if args.resume is None:
            require_supervised_authority(True, PermissionPolicy(config.permissions))
            if args.message is None or args.output is None:
                raise WorkspaceHostError("workflow_profile_required")
        elif args.message is not None or args.output is not None or args.inputs or args.enable_write:
            raise WorkspaceHostError("workflow_resume_read_only")
        human = TerminalWorkflowHuman()
        model = provider_from_config(config) if args.resume is None else None
        with SupervisedWorkspaceWorkflow(config=config, human=human, model=model,
                                          enable_write=args.enable_write) as host:
            if args.resume is not None:
                result = host.resume(args.resume)
            else:
                result = host.run(args.message, WorkflowProfile(tuple(args.inputs), args.output,
                    max_bytes=args.max_bytes, max_seconds=args.max_seconds))
    except WorkspaceHostError as exc:
        result.update(ok=False, status=exc.category)
    except (KeyboardInterrupt, SystemExit):
        result.update(ok=False, status="cancelled")
    except Exception:
        # Close/save failures cannot erase previously obtained action truth.
        result.update(ok=False, status="storage_evidence_failure")
    if args.as_json:
        print(json.dumps(result, ensure_ascii=True))
    else:
        print(result["status"] + (" (historical observation)" if result.get("historical") else ""))
        if result.get("workflow_id"):
            print(result["workflow_id"])
        if result.get("verification") is not None:
            print("Verified at: " + result["verification"]["observed_at"])
    return 0 if result["ok"] else 1
