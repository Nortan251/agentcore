# Windows 11 Notepad control v1

`agentcore chat --enable-desktop` adds one zero-argument tool, `open_notepad`,
to an ordinary conversation. The model must support structured tool calls.
The offline `echo` provider cannot demonstrate this action.

## Windows demonstration

Use a normal, non-administrator PowerShell terminal on an active, unlocked
Windows 11 workstation with Notepad installed. A disconnected RDP session,
locked desktop, Session 0, Windows Server, Windows 10, Linux and macOS are
unsupported. The service never switches sessions or elevates.

In a fresh directory:

```powershell
git clone --branch codex/jarvis-notepad-v1 https://github.com/Nortan251/agentcore.git
Set-Location agentcore
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
Copy-Item agent.example.toml agent.local.toml
```

Edit `agent.local.toml`:

- Keep the `[model]` values from your working AgentCore configuration: a real
  tool-capable provider, its model name and endpoint, and the environment-variable
  name holding its credential if required. Do not put credentials in the file.
- Set `[permissions] level = "confirm"`. Keep default capability requirements;
  neither `app.control` nor `process.spawn` may be explicitly blocked for this demo.
- Set `[conversation] tools_enabled = true`.
- For a demonstration restricted to Notepad, set `[tools] enabled = ["open_notepad"]`
  and leave `disabled = []`. Existing configurations may instead add
  `open_notepad` to their existing allowlist. Empty `enabled` allows all registered
  tools, as before. No new tool can be enabled through model output.

Start the existing conversation interface:

```powershell
.\.venv\Scripts\agentcore.exe --config agent.local.toml chat --enable-desktop
```

Then:

```text
You > Jarvis, open Notepad.
[Desktop approval] Open Notepad? Type 'open notepad' to allow: open notepad
...
[Desktop] Windows accepted the Notepad launch. A visible window is not verified.
```

The **operator must look at the desktop and verify that Notepad appeared**.
Record this separately as LIVE DESKTOP PASS or FAIL, with the branch commit SHA,
Windows build, and whether Notepad was already running. This step has not been
verified by automated tests. Existing Notepad preferences may restore tabs or
reuse an application instance; the action does not promise a new blank window.

An empty answer, `no`, EOF or Ctrl+C at the approval prompt denies the action.
An ordinary user message saying “open Notepad” is not the operator's approval.
At OBSERVE/PROPOSE the policy denies the action. At AUTONOMOUS, the existing
policy may permit it without prompting. Nothing here lowers capability thresholds.
JSON mode and redirected stdin never prompt or auto-approve; at CONFIRM their
request is denied. Conversation success/CLI exit code alone is not launch proof:
read the authoritative `[Desktop]` notice (or JSON `notices`), including when
the model fails or its prose is inaccurate.

Resume with the same configuration and explicit opt-in:

```powershell
.\.venv\Scripts\agentcore.exe --config agent.local.toml chat --enable-desktop --session <session-id>
```

History, memory, and a saved session never enable the service or restore approval.
Without `--enable-desktop`, ordinary Agent construction, chat and doctor retain
their default desktop-disabled behavior. The legacy `features.computer_control`
placeholder is not an authorization switch.

## Execution and service boundary

The path is `ConversationRuntime -> NotepadConversationRunner -> ToolRegistry ->
validation -> exact trusted ActionAdapter registration -> PermissionPolicy ->
ToolExecutor -> OpenNotepadTool -> ComputerControlAdapter -> separate service`.
Only the exact host-bound Notepad tool can reach the synchronous console approval
gate from conversation; other tools retain their existing approval escalation.
Both `APP_CONTROL` and `PROCESS_SPAWN` are required. The registry execution lease
and existing policy-generation lease cover dispatch and service completion,
without holding a metadata mutex across prompts or I/O. Revocation during the
approval/checkpoint interval blocks dispatch, including replace-and-restore of
the same policy object. Replacement while physical I/O is leased fails busy.

Opening an application is explicitly `EPHEMERAL_CONTROL` in the
[ActionJournal contract](task-43.1-action-journal.md#n-which-operations-require-journaling).
The static registration is bound to the exact tool object/name/version/capabilities;
it does not journal, reconcile or resume a launch. No other desktop action is
classified by this registration.

The service is installed with AgentCore. Each health/action request starts the
same interpreter in isolated mode with the fixed installed `desktop/service.py`
file, using private stdio and a hidden service console. There is no shared
listener, port, auth-key file, network endpoint, or service daemon to configure.
It runs as the current operator in the inherited session. This transport trusts
the installed Python/AgentCore/pywin32 code and the operator account; it is not
an OS sandbox against malicious code running as that account.

The entire request is at most 256 bytes and exactly
`{"v":1,"operation":"health"}` or `{"v":1,"operation":"open_notepad"}`.
Extra keys, arguments, paths, commands and alternate operation names are refused.
The response is a bounded versioned categorical status. Only the service imports
native desktop/process APIs. It checks Windows 11 workstation version, active
nonzero WTS session, `WinSta0` and the accessible `Default` input desktop, then
uses native `CreateProcess` with an absolute OS system-directory `notepad.exe`
path, no command line, no inherited handles and an explicit default desktop.
Both child processes receive an allowlisted environment, excluding model keys
and Python startup variables. No PATH search, shell, file opening, typing,
window closing, screenshots or arbitrary executable parameters are exposed.

The native process handle is briefly checked for immediate failure and closed;
it is not a window-visibility receipt. Modern Notepad may forward activation
to another process. Native creation failure is reported as failure. A timeout,
malformed/lost response or post-creation error preserves uncertainty and blocks
further launch dispatch through that adapter for the rest of the chat process.
The conversation runner also latches against further Notepad dispatch when the
executor loses its result or returns an unclassified failure, including after
service acceptance. A missing invocation alone is not proof of non-launch:
only explicit pre-dispatch refusal evidence supports that notice. Unknown
outcomes tell both the operator and model that Notepad may have opened.
There is no automatic retry, including duplicate calls in one response or later
rounds and user turns in that running chat. After uncertainty, inspect the desktop before
explicitly starting a new chat process and requesting another launch.

## Verification boundaries

`tests/test_desktop.py` tests the real isolated stdio health/refusal protocol,
registration, both capability gates, operator approval/denial, normal ordinary
chat, status rendering after a model failure, fixed native launch arguments
with `CreateProcess` mocked, native failures, transport uncertainty, and policy
and registry races. No test starts Notepad. The existing Windows full-suite
workflow runs these tests without a workflow change. Windows Server CI proves
noninteractive contracts and unsupported-host refusal, not Windows 11 GUI launch.
Native launch-contract tests are explicitly skipped on non-Windows hosts.

API references: [OpenInputDesktop](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-openinputdesktop),
[CreateProcessW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw).
