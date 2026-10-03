"""Trusted CLI host for the existing isolated Browser Read composition."""

from contextlib import contextmanager
import errno
import json
import os
import re
import sys

from agentcore.config.loader import load_config
from agentcore.conversation.errors import ConversationError
from agentcore.errors import ConfigError
from agentcore.models.errors import ModelError
from agentcore.models.factory import provider_from_config
from agentcore.util.ids import new_id

from .composition import BrowserAgentComposition
from .config import BrowserConfig
from .errors import BrowserError, BrowserFailure
from .session import safe_text

_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_MAX_REPLY_CHARS = 8000
_MESSAGES = {
    BrowserFailure.DISABLED.value: "Browser access is disabled.",
    BrowserFailure.NETWORK_BOUNDARY.value: "The browser isolation boundary is unavailable.",
    BrowserFailure.NAVIGATION_NOT_AUTHORIZED.value: "Navigation is not authorized; grant the exact URL with --allow-url.",
    BrowserFailure.DESTINATION.value: "The destination is denied.",
    BrowserFailure.DNS.value: "Browser DNS resolution failed.",
    BrowserFailure.LAUNCH.value: "Browser launch failed; check the browser extra and Chromium installation.",
    BrowserFailure.TIMEOUT.value: "The browser operation timed out.",
    BrowserFailure.CLOSED.value: "The browser page is unavailable or closed.",
    BrowserFailure.CRASH.value: "The browser stopped unexpectedly.",
    BrowserFailure.PROVIDER.value: "The browser backend failed.",
    BrowserFailure.CLEANUP.value: "Browser cleanup could not be verified.",
    BrowserFailure.OWNERSHIP.value: "Browser runtime ownership is invalid.",
    BrowserFailure.CONTRACT.value: "Browser composition or navigation grants are invalid.",
    BrowserFailure.FETCH.value: "The browser fetch failed.",
    BrowserFailure.RESPONSE.value: "The browser response was rejected.",
    BrowserFailure.SIZE.value: "The browser response exceeds the allowed size.",
    BrowserFailure.REDIRECT.value: "The browser redirect was rejected.",
    "model_configuration_failed": "Model configuration failed; set model.provider and model.name using --config or AGENTCORE_* variables.",
    "model_failed": "The configured model could not complete the turn; check its configuration and credential environment variable.",
    "browser_session_invalid": "The browser session is missing, invalid, or belongs to another conversation type.",
    "browser_session_busy": "The browser session is already in use; try again after its current turn finishes.",
    "browser_session_storage_failed": "The browser session could not be saved or read.",
    "browser_prompt_invalid": "The browser prompt is empty or exceeds the configured message bound.",
    "browser_tool_failed": "A proposed browser tool call was refused or failed.",
}


class _SessionBusy(Exception):
    pass


@contextmanager
def _session_lease(directory, session_id):
    # The caller validates bounded IDs before any storage access. Keep the
    # lock inode after release: unlinking it would allow concurrent owners.
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ("session-" + session_id + ".lock")).open("a+b") as handle:
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise _SessionBusy from None
            raise
        owner_pid = os.getpid()
        try:
            # Native byte locks can cover EOF. Initialize only after owning
            # the lease, so contenders never write into a locked byte.
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b"0")
                handle.flush()
            yield
        finally:
            # A forked child must not unlock the parent's shared descriptor.
            if os.getpid() == owner_pid:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _turn_error(result):
    # Only executor-owned categorical metadata enters CLI output. Never render
    # model call names/arguments, refusal content or exception messages here.
    failures = []
    for outcome in result.tool_outcomes:
        if not outcome.ok:
            invocation = outcome.invocation
            kind = (invocation.result.metadata or {}).get("kind") if invocation else None
            failures.append(kind if kind in _MESSAGES else "browser_tool_failed")
    if BrowserFailure.CLEANUP.value in failures:
        return BrowserFailure.CLEANUP.value
    return failures[0] if failures else "model_failed" if not result.ok else None


def command_browser(args):
    payload = {"ok": False, "session_id": None, "reply": None, "tools_executed": 0}
    error = None
    try:
        config = load_config(args.config)
        # Reuse the normal provider factory. Missing/bad configuration fails
        # before constructing browser backends or opening session storage.
        model = provider_from_config(config)
        if not args.prompt.strip() or len(args.prompt) > config.conversation.max_message_chars:
            error = "browser_prompt_invalid"
        elif args.session_id is not None and not _SESSION_ID.fullmatch(args.session_id):
            error = "browser_session_invalid"
        else:
            directory = config.canonical_workspace() / ".browser_sessions"
            session_id = args.session_id or new_id("conv")
            # New turns also hold their lease before runtime's best-effort
            # autosave can make the transcript visible to a resume process.
            with _session_lease(directory, session_id), BrowserAgentComposition(
                model=model, config=BrowserConfig(enabled=True),
                permissions=config.permissions, conversation_config=config.conversation,
                sessions_directory=directory,
            ) as browser:
                conversation = browser.conversation()
                if not browser.available:
                    error = BrowserFailure.NETWORK_BOUNDARY.value
                elif conversation.sessions.exists(session_id) != (args.session_id is not None):
                    error = "browser_session_invalid"
                else:
                    # Composition.start checks the browser-only marker. Do not
                    # use ordinary chat's fallback/new-session resume helper.
                    session = conversation.start(session_id=session_id)
                    if session.id != session_id:
                        error = "browser_session_invalid"
                    else:
                        result = conversation.send(args.prompt, session=session,
                                                   navigation_urls=tuple(args.allow_url))
                        payload["tools_executed"] = result.tools_executed
                        error = _turn_error(result)
                        # Runtime persistence is best-effort; verify the host's
                        # required durable save rather than promise a lost id.
                        if not conversation.save(result.session):
                            error = "browser_session_storage_failed"
                        else:
                            payload["session_id"] = session.id
                        if error is None:
                            reply = result.reply.content
                            if len(reply) > _MAX_REPLY_CHARS:
                                reply = reply[:_MAX_REPLY_CHARS - 14] + "… [truncated]"
                            payload["reply"] = safe_text(reply, _MAX_REPLY_CHARS)
    except _SessionBusy:
        error = "browser_session_busy"
    except BrowserError as exc:
        error = exc.failure.value
    except (ConfigError, ModelError):
        error = "model_configuration_failed"
    except ConversationError:
        error = "browser_session_invalid"
    except OSError:
        error = "browser_session_storage_failed"

    # Publication follows context-manager cleanup: uncertainty overrides reply.
    payload["ok"] = error is None
    if error == "browser_session_storage_failed":
        payload["session_id"] = None
    if error:
        payload["reply"] = None
        payload["error"] = error
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=True))
    elif error:
        print(_MESSAGES[error], file=sys.stderr)
    else:
        print(payload["reply"])
    return 0 if payload["ok"] else 1
