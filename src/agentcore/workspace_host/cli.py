"""Categorical public output; model prose and filesystem content stay private."""

import json
import re
import sys

from agentcore.config.loader import load_config
from agentcore.conversation.errors import ConversationError
from agentcore.errors import ConfigError
from agentcore.models.errors import ModelError
from agentcore.models.factory import provider_from_config
from agentcore.permissions.policy import PermissionPolicy
from .composition import WorkspaceAgentComposition
from .errors import WorkspaceHostError, require_authority


def command_workspace(args):
    payload = {"ok": False, "session_id": None, "reply": None, "tools_executed": 0,
               "actions": [], "reply_generated": False}
    error = None
    host = None
    try:
        # The absence of this invocation's opt-in must be decided even before
        # config canonicalization, provider construction or storage access.
        if args.enable_write is not True:
            raise WorkspaceHostError("workspace_write_opt_in_required")
        config = load_config(args.config)
        require_authority(True, PermissionPolicy(config.permissions))
        if not args.message.strip() or len(args.message) > config.conversation.max_message_chars:
            raise WorkspaceHostError("workspace_prompt_invalid")
        if args.session_id is not None and re.fullmatch(r"conv_[0-9a-f]{12}", args.session_id) is None:
            raise WorkspaceHostError("workspace_session_invalid")
        model = provider_from_config(config)
        with WorkspaceAgentComposition(config=config, enable_write=True, model=model) as host:
            conversation = host.conversation()
            try:
                try:
                    session = conversation.start(session_id=args.session_id)
                except ConversationError:
                    raise WorkspaceHostError("workspace_session_invalid") from None
                result = conversation.send(args.message, session=session)
                payload["tools_executed"] = result.tools_executed
                payload["reply_generated"] = result.reply is not None
                if any(not outcome.ok for outcome in result.tool_outcomes):
                    error = "workspace_tool_failed"
                elif not result.ok:
                    error = "workspace_model_failed"
                # Keep best-effort autosave failures sticky, even if a later
                # explicit save succeeds. Saving never rolls back an effect.
                saved = conversation.save(result.session)
                if saved:
                    payload["session_id"] = result.session.id
                if not saved or conversation.save_failed:
                    error = error or "workspace_session_storage_failed"
            except WorkspaceHostError:
                raise
            except Exception:
                error = "workspace_turn_failed"
            finally:
                payload["tools_executed"] = max(payload["tools_executed"],
                                                sum(inv.ok for inv in host._invocations))
                try:
                    payload["actions"] = host.action_truth()
                except Exception:
                    error = "workspace_action_truth_unavailable"
                states = {entry["state"] for entry in payload["actions"]}
                if "reconcile_required" in states or "in_flight" in states:
                    error = "workspace_action_requires_operator"
                elif states - {"succeeded"}:
                    error = error or "workspace_tool_failed"
    except WorkspaceHostError as exc:
        error = exc.category
        if exc.action_ids:
            payload["action_ids"] = list(exc.action_ids)
    except (ConfigError, ModelError):
        error = "workspace_configuration_failed"
    except Exception:
        error = "workspace_storage_failed"
    payload["ok"] = error is None
    if error:
        payload["error"] = error
    # Model prose can repeat protected write content (including from a resumed
    # read). No string scrubber can guarantee otherwise. V1 deliberately
    # exposes only the reply's existence; its text is in the private session.
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=True))
    else:
        print(error if error else "Workspace turn completed; reply retained in the private session.",
              file=sys.stderr if error else sys.stdout)
        for entry in payload["actions"]:
            print(f"{entry['action_id']}: {entry['state']} ({entry['effect']})")
    return 0 if payload["ok"] else 1
