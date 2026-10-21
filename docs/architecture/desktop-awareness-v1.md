# Windows desktop awareness v1

`agentcore --config agent.local.toml chat --enable-desktop` can expose the
zero-argument `get_foreground_app` alongside `open_notepad`. Both require current
explicit desktop opt-in, `conversation.tools_enabled = true`, and registration
through the configured tool allowlist. No desktop tools are registered by
ordinary `Agent()` or chat without `--enable-desktop`. Empty `tools.enabled`
retains its existing meaning: allow all registered tools. For a restricted
observation demonstration, explicitly list only `get_foreground_app`.

## Authority and privacy

The path is the existing `ConversationRuntime -> NotepadConversationRunner ->
ToolRegistry -> validation -> PermissionPolicy -> ToolExecutor ->
GetForegroundAppTool -> ComputerControlAdapter -> isolated desktop service`.
Observation is an `ExternalReadTool` requiring only `SCREEN_OBSERVE`, permitted
by the existing read policy at OBSERVE unless explicitly blocked. The external
read contract accepts either `NET_READ` or `SCREEN_OBSERVE` and still rejects
all mutating capabilities. Registry identity and policy-generation leases span
dispatch, native I/O, response validation and transport cleanup. No policy or
registry metadata mutex spans that I/O. Policy replacement during a lease fails
busy; replacement before lease acquisition invalidates the evaluated generation,
including replacement and restoration of the same policy object.

`open_notepad` keeps its exact tool registration, APP_CONTROL/PROCESS_SPAWN
requirements, console approval, launch uncertainty and retry suppression.
Foreground observation neither uses its approval phrase nor clears uncertainty.
Read failures need no durable journal or launch-recovery record. History and
saved sessions never restore desktop authorization.

Each call launches the same fixed service file with isolated Python, a hidden
console, private one-request stdio and an allowlisted environment excluding
provider credentials and Python startup settings. There is no new listener,
daemon, endpoint, shell, executable argument, privilege change or session switch.
The installed runtime and current operator account remain trusted, as in PR #47.

The service checks Windows 11 workstation, active nonzero WTS session, WinSta0,
and an accessible Default input desktop. It uses `GetForegroundWindow` and
`GetWindowThreadProcessId`, opens only that process with
`PROCESS_QUERY_LIMITED_INFORMATION`, and checks the opened process token's
session against its own before querying the image. `QueryFullProcessImageNameW`
uses a fixed 32,768-character native buffer. The path stays inside the service
and is immediately reduced to a fixed executable-name label:

| Executable basename | Returned app label |
| --- | --- |
| `notepad.exe` | Notepad |
| `chrome.exe` | Google Chrome |
| `msedge.exe` | Microsoft Edge |
| `code.exe` | Visual Studio Code |
| `explorer.exe` | File Explorer |
| `WindowsTerminal.exe` | Windows Terminal |

Explorer is recognized only for an Explorer folder-window class, excluding the
taskbar and desktop shell. Unrecognized executables and shared hosts such as
ApplicationFrameHost return `unknown`. A label describes the observed executable
name; it does **not** authenticate a publisher or binary. A renamed executable
can therefore match a label. Never use this descriptive output as security or
execution authority. Publisher authentication and child-window disambiguation
are outside v1.

Before returning a label, the service rechecks input-desktop availability,
foreground HWND/thread/PID stability and process liveness. Changed focus or
process exit returns `unknown`; a locked or detached desktop is unavailable.
This is a point-in-time observation; focus can change after it completes.
Process handles and token handles close on all exits. Access failures expose
only `inaccessible`, never native exception details.

No title, document name, URL, raw path, PID, screen content, screenshot, window
enumeration or app-provided free text crosses the service boundary. There is
no polling, background observation, cached identity or automatic retry.

## Closed protocol

The additive v1 request is exactly
`{"v":1,"operation":"get_foreground_app"}`. Existing `health` and `open_notepad`
requests and categorical response schemas are unchanged. Requests and responses
are at most 256 bytes; duplicate keys, extra keys, wrong types, invalid versions
and unknown operations are rejected. Both endpoints reject malformed JSON.
The adapter captures at most 257 bytes before rejecting excess output; the
10-second timeout kills and reaps the owned service and joins its I/O worker.

Observation responses contain `v`, `status`, `app`; `unknown` may additionally
carry a strictly validated `reason` from a fixed operator-diagnostic enum:

```json
{"v":1,"status":"observed","app":"Notepad"}
{"v":1,"status":"unknown","app":null}
{"v":1,"status":"unknown","app":null,"reason":"foreground_changed"}
```

Only `observed` may carry one of the six fixed labels. Every other status has
`app: null`. `unknown` means the read completed without a safe recognized label.
`inaccessible` means native access failed or a different session was detected.
`desktop_unavailable`, `unsupported_os` and `service_unavailable` retain distinct
meanings. Lost, malformed, oversized or nonzero-exit service responses become
`service_unavailable`, with no app identity inferred. Invalid launch responses
still become `launch_unknown` and latch against retries.

Unknown reasons distinguish `no_foreground_window`, `window_process_unavailable`,
`image_query_invalid`, `executable_not_allowlisted`, `shared_application_host`,
`console_host`, `explorer_shell`, `foreground_changed`, and
`foreground_process_exited`. No executable name outside the app allowlist,
window class, title, path or PID is exposed. Only unknown responses may carry
one of these reasons; extra fields, other statuses with a reason, arbitrary
strings and wrong types fail closed. Legacy three-field unknown responses
remain accepted. The adapter retains the reason in typed tool metadata and
the operator's JSON `desktop_observations`; model-visible tool content keeps
only `{status, app}`. These reasons never grant authority or trigger retries.

The final stability check compares the returned thread ID and process ID
individually. Comparing the native return sequence directly to a Python tuple
can falsely report a focus change despite identical IDs. Regressions cover
both list and tuple return sequences, actual changes, process exit, and modern
packaged Notepad image-path classification. Shared hosts are still unknown;
the service never guesses their hosted application from a title or class.

The console's `[Desktop]` observation notices are derived from tool evidence,
independently of generated model prose. JSON turns containing observation calls
also include `desktop_observations`, a list of bounded `{status, app}` records.
Pre-execution denials and executor result loss become `not_observed` in that
list. Model prose remains separately under `reply`; it is never evidence.

## Operator demonstration

Install this branch with `python -m pip install -e ".[dev]"` and reuse the
working local NVIDIA provider block, including `api_key_env = "NVIDIA_API_KEY"`.
Keep the key only in the invoking terminal's environment. Do not copy it into
TOML, transcripts, commands, evidence files or Git. Example non-secret settings:

```toml
[model]
provider = "openai_compatible"
name = "z-ai/glm-5.3-flash"
base_url = "https://integrate.api.nvidia.com/v1"
api_key_env = "NVIDIA_API_KEY"

[permissions]
level = "confirm"

[conversation]
tools_enabled = true

[tools]
enabled = ["get_foreground_app", "open_notepad"]
disabled = []
```

Typing in the chat terminal makes that terminal the foreground application.
For a truthful manual demonstration, submit the question after a short delay
and manually focus the target application while the delay runs. In PowerShell,
with the config path resolved before switching focus:

```powershell
$demoConfig = (Resolve-Path agent.local.toml).Path
Write-Host 'Select Notepad now; keep it selected until the result appears.'
Start-Sleep -Seconds 8
python -m agentcore.cli --config $demoConfig chat --enable-desktop `
  --message 'Jarvis, what application am I using right now? Use get_foreground_app.' --json
```

Do not return to the terminal until the observation finishes. Repeat with
VS Code, Chrome, Edge, File Explorer or Windows Terminal. Repeat with an
unrecognized application for truthful `unknown`; do not change security
settings merely to manufacture an inaccessible window. If naturally
inaccessible native access cannot be demonstrated, record that part NOT RUN.
Mocked access-denial tests remain separate from native evidence.

### Separate provider and native checks

From the checkout root, run a provider-only diagnostic with the same config:

```powershell
python src/agentcore/desktop/diagnostics.py provider --config agent.local.toml
```

This sends one fixed tiny completion request through the existing provider
factory and HTTP client, with one total attempt, 128 output tokens, a 20-second
socket timeout and a 30-second child-process limit. These are diagnostic-only
overrides; production and saved provider defaults are unchanged. The child
disables logging and inherited TLS key logging. HTTP redirects are refused;
responses are bounded to 64 KiB and error bodies are not read. Only fixed
categories, status, phase, timing and budget metadata are returned. No provider
text, reasoning text, headers or credentials are printed or saved.

`endpoint` is a separate unauthenticated GET reachability check. An HTTP response
does not prove authentication or completion availability. `provider-tools` is an
optional separate one-attempt test asking for a `diagnostic_noop` call; nothing
it returns is executed. `tool_call_returned` proves this sampled structured call
worked. `tool_call_not_returned` does not prove tool calls are unsupported.
`model_response_budget_exhausted` distinguishes a length-limited answer from
connection failure. A timeout alone cannot identify throttling, invalid
credentials, configuration rejection or the server's reason for delay; use
observed HTTP status and response phase, and retain uncertainty otherwise.

For a model-independent foreground smoke test, manually select the target
window during a short delay:

```powershell
Start-Sleep -Seconds 8
python src/agentcore/desktop/diagnostics.py native --config agent.local.toml --enable-desktop
```

This needs no API key or provider connectivity. It registers only the existing
`get_foreground_app` tool and calls `ToolExecutor` with the config's current
permission policy. Desktop opt-in, conversation tool enablement, the original
tool allowlist and explicit screen-observation denials are all enforced. It
never constructs a model or approves Notepad. Results remain unconfirmed until
the operator verifies them. Native results do not prove a model used a tool.

Failed conversation JSON now retains a fixed `error_category` (authentication,
rate limit, timeout, connection, configuration, invalid response, budget exhaustion,
HTTP failure, or generic model failure), while keeping the existing generic
human-readable error. Tool evidence collected before a later model failure
remains available separately; an error must not erase an actual observation.

Record the exact commit, Windows build, expected app chosen by the operator,
`desktop_observations`, generated `reply`, and the operator's explicit
confirmation separately. A model answer, automatic native probe or green test
suite never earns LIVE DESKTOP PASS. That status requires actual operator
confirmation for the observations. Use `open_notepad` only through its existing
operator-approval flow; this demonstration does not need to launch anything.

## Verification limits

Deterministic tests mock Windows APIs on every platform and cover identification,
native cleanup, unknown/inaccessible results, inactive/locked/detached/session-0
desktops, cross-session refusal, unsupported hosts, request/response schema and
size bounds, service timeout, policy/registry races, concurrent calls, allowlists,
privacy through model requests/logging/persistence, and unchanged Notepad
uncertainty. Actual Windows tests also exercise isolated service stdio.
Windows Server CI validates refusal and mocked native contracts; it does not
prove Windows 11 interactive app recognition or manual operator confirmation.

Native API references:
[GetForegroundWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getforegroundwindow),
[GetWindowThreadProcessId](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowthreadprocessid),
[QueryFullProcessImageNameW](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-queryfullprocessimagenamew).
