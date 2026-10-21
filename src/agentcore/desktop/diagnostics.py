"""Explicit, bounded operator diagnostics; no model output authorizes native I/O."""

from __future__ import annotations

import argparse
from dataclasses import replace
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

# The isolated child imports only this checkout, independent of editable installs.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agentcore.config.loader import load_config
from agentcore.models.errors import ModelResponseError, model_error_category
from agentcore.models.factory import create_provider
from agentcore.models.messages import Message, ModelRequest
from agentcore.models.transport import HttpResponse, UrllibTransport


ENDPOINT = "https://integrate.api.nvidia.com/v1"
TIMEOUT = 20
TOKEN_BUDGET = 128
WALL_TIMEOUT = 30
BODY_LIMIT = 65536


class DiagnosticTransport:
    """Reuse the nonredirecting transport seam, with bounded JSON body capture."""
    def __init__(self):
        self.calls = 0
        self.status = None
        self.phase = "not_started"
        self.finish_reason = None
        self.reasoning_present = False

    def post_json(self, url, payload, headers, timeout):
        self.calls += 1
        self.phase = "connecting_or_waiting_for_headers"
        response = UrllibTransport().open_stream(url, payload, headers, timeout)
        try:
            self.status = response.status
            self.phase = "reading_response_body"
            if not 200 <= response.status < 300:
                return HttpResponse(response.status, b"{}")
            body = bytearray()
            while len(body) <= BODY_LIMIT:
                chunk = response.read(min(4096, BODY_LIMIT + 1 - len(body)))
                if not chunk:
                    break
                body.extend(chunk)
            if len(body) > BODY_LIMIT:
                raise ModelResponseError("diagnostic response exceeded its bound")
            self.phase = "response_received"
            try:
                data = json.loads(body)
            except ValueError:
                raise ModelResponseError("diagnostic response was not JSON") from None
            choices = data.get("choices") if type(data) is dict else None
            if type(choices) is list and choices and type(choices[0]) is dict:
                choice = choices[0]
                finish = choice.get("finish_reason")
                self.finish_reason = finish if type(finish) is str and finish in {"stop", "length", "tool_calls", "content_filter"} else None
                message = choice.get("message")
                self.reasoning_present = type(message) is dict and bool(message.get("reasoning_content"))
            return HttpResponse(response.status, bytes(body))
        finally:
            response.close()


def provider_check(config, *, tools=False):
    if (config.model.provider != "openai_compatible" or config.model.base_url != ENDPOINT
            or config.model.name != "z-ai/glm-5.3-flash" or config.model.api_key_env != "NVIDIA_API_KEY"):
        return {"category": "model_configuration", "attempts": 0}
    if not config.model.resolve_api_key():
        return {"category": "credential_unavailable", "attempts": 0}
    model = replace(config.model, timeout_seconds=TIMEOUT, max_output_tokens=TOKEN_BUDGET, max_retries=1)
    transport = DiagnosticTransport()
    request = ModelRequest(messages=[Message.user("Reply with exactly OK.")])
    if tools:
        request.messages = [Message.user("Call diagnostic_noop once with no arguments. Do not answer in prose.")]
        request.tools = [{"name": "diagnostic_noop", "description": "Return no data; diagnostic only, never executed.",
                          "parameters": [], "read_only": True, "capabilities": []}]
    try:
        response = create_provider(model, transport=transport).generate(request)
        category = "provider_ok"
        if tools:
            valid = (len(response.tool_calls) == 1 and response.tool_calls[0].name == "diagnostic_noop"
                     and response.tool_calls[0].arguments == {})
            category = "tool_call_returned" if valid else "tool_call_not_returned"
        if response.finish_reason.value == "length":
            category = "model_response_budget_exhausted"
    except Exception as error:
        category = model_error_category(error)
    return {"category": category, "attempts": transport.calls, "http_status": transport.status,
            "phase": transport.phase, "finish_reason": transport.finish_reason,
            "reasoning_present": transport.reasoning_present, "timeout_seconds": TIMEOUT,
            "max_output_tokens": TOKEN_BUDGET}


def endpoint_check():
    """Unauthenticated reachability only; no completion or credential test."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args): return None
    try:
        response = urllib.request.build_opener(NoRedirect()).open(ENDPOINT + "/models", timeout=TIMEOUT)
    except urllib.error.HTTPError as error:
        response = error
    except TimeoutError:
        return {"category": "endpoint_timeout"}
    except Exception:
        return {"category": "endpoint_connection_failed"}
    try:
        return {"category": "endpoint_http_response", "http_status": response.status,
                "authentication_tested": False}
    finally:
        response.close()


def native_check(config, *, enabled):
    from agentcore.desktop.service import APP_NAMES, OBSERVATION_STATUSES, UNKNOWN_REASONS
    from agentcore.integrations.computer_control import ComputerControlAdapter
    from agentcore.integrations.base import IntegrationStatus
    from agentcore.permissions.policy import PermissionPolicy
    from agentcore.tools.executor import ToolExecutor
    from agentcore.tools.registry import ToolRegistry

    if not enabled or not config.conversation.tools_enabled:
        return {"category": "desktop_not_enabled"}
    if not config.tools.is_allowed("get_foreground_app"):
        return {"category": "tool_disabled"}
    adapter = ComputerControlAdapter({"enabled": True})
    try:
        if adapter.connect() is not IntegrationStatus.CONNECTED:
            return {"category": "desktop_service_unavailable"}
        registry = ToolRegistry(config.tools)
        tool = next(tool for tool in adapter.provided_tools() if tool.name == "get_foreground_app")
        assert registry.register(tool)
        result = ToolExecutor(registry, PermissionPolicy(config.permissions)).invoke(tool.name, {}).result
        category = result.metadata.get("kind")
        allowed = OBSERVATION_STATUSES | {"permission_denied", "validation", "tool_registration_changed"}
        category = category if category in allowed else "native_failure"
        output = result.output
        app = (output.get("app") if result.ok and category == "observed" and type(output) is dict else None)
        if app is not None and app not in APP_NAMES.values():
            return {"category": "native_failure"}
        observation = {"status": category, "app": app}
        reason = result.metadata.get("observation_reason")
        if category == "unknown" and type(reason) is str and reason in UNKNOWN_REASONS:
            observation["reason"] = reason
        return {"category": category, "desktop_observations": [observation],
                "model_used": False, "operator_confirmed": False}
    finally:
        adapter.disconnect()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("endpoint", "provider", "provider-tools", "native"))
    parser.add_argument("--config", required=True)
    parser.add_argument("--enable-desktop", action="store_true")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    previous = logging.root.manager.disable
    logging.disable(sys.maxsize)
    started = time.monotonic()
    try:
        if not args.worker:
            command = [sys.executable, "-I", str(Path(__file__).resolve()), args.operation,
                       "--config", str(Path(args.config).resolve()), "--worker"]
            if args.enable_desktop: command.append("--enable-desktop")
            environment = dict(os.environ)
            environment.pop("SSLKEYLOGFILE", None)
            try:
                completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                           env=environment, timeout=WALL_TIMEOUT, check=False)
                result = json.loads(completed.stdout) if len(completed.stdout) <= 4096 else None
                if completed.returncode or type(result) is not dict:
                    result = {"category": "diagnostic_process_failed"}
            except subprocess.TimeoutExpired:
                result = {"category": "diagnostic_wall_timeout", "request_outcome": "unknown", "retry_attempted": False}
            except KeyboardInterrupt:
                result = {"category": "operator_cancelled", "request_outcome": "unknown", "retry_attempted": False}
            except Exception:
                result = {"category": "diagnostic_process_failed"}
        else:
            config = load_config(args.config)
            if args.operation == "endpoint": result = endpoint_check()
            elif args.operation == "native": result = native_check(config, enabled=args.enable_desktop)
            else: result = provider_check(config, tools=args.operation == "provider-tools")
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        print(json.dumps(result))
        return 0
    except BaseException:
        print(json.dumps({"category": "diagnostic_configuration_or_process_error"}))
        return 1
    finally:
        logging.disable(previous)


if __name__ == "__main__":
    raise SystemExit(main())
