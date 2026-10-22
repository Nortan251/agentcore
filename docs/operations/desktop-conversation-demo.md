# Model-driven Windows foreground demonstration

Use Windows 11 and the normal `agentcore chat --enable-desktop` runtime. The
launcher delays submission for eight seconds so the operator can focus an
already-open target. It sends the natural question, without a tool name:
`Jarvis, what application am I using right now?`

## Local model profile

Keep the credential in the invoking terminal's environment. Never put its value
in TOML, a command, a transcript, or an evidence record. An operator-local
`agent.local.toml` can select this NVIDIA alternative:

```toml
workspace_dir = "./.agent_workspace"

[model]
provider = "openai_compatible"
name = "nvidia/nemotron-3.5-lightning-30b-a3b"
base_url = "https://integrate.api.nvidia.com/v1"
api_key_env = "NVIDIA_API_KEY"
temperature = 0.0
max_output_tokens = 128
timeout_seconds = 20.0
max_retries = 1
enable_thinking = false

[permissions]
level = "confirm"

[tools]
enabled = ["get_foreground_app"]

[conversation]
tools_enabled = true
max_tool_rounds = 1
max_tool_calls_per_round = 1

[logging]
level = "CRITICAL"
```

The optional `enable_thinking` setting becomes
`chat_template_kwargs.enable_thinking`; omission preserves the server's thinking
default. Use it only with a backend that supports that setting. Synchronous
generation explicitly requests `stream=false`, while the existing streaming
API still requests `stream=true`. No provider-wide model, timeout, retry, or
permission default changes.

NVIDIA documents the [streaming default and request contract](https://docs.api.nvidia.com/nim/reference/nvidia-nemotron-3-5-lightning-30b-a3b-infer)
and the [thinking-off option](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Lightning-30B-A3B-NVFP4#api-client).
The operator's bounded preflight completed three HTTP 200 requests with this
profile: complete text, one zero-argument memory-only diagnostic call through
the registry/policy/executor, and an accurate final result response. That is
provider evidence, not a Windows observation. GLM's earlier pre-header timeouts
remain unexplained; success through the same NVIDIA endpoint and key narrows
the investigation toward model-specific behavior without identifying its cause.

## Run and inspect

From the checkout, with the existing key available:

```powershell
& .\tools\Start-Desktop-Demo.ps1 -Config .\agent.local.toml
```

Open Notepad manually first. During the eight-second delay, select it and keep
it foreground until the result appears. Return to the terminal to answer the
two confirmation questions. For an unsupported application, repeat with
`-ExpectedApp unknown`; confirm only if the native result is truthfully unknown
and the model accurately explains that the check completed without safely
identifying the app. A successful unknown result is not permission denial or
tool unavailability. Reject an answer that claims missing access or invents
an app identity.

When the foreground tool is enabled, desktop chat keeps this distinction in
the system prompt through the final response, including when the tool-round
budget is exhausted and no further tools are offered. Operator-only diagnostic
reasons remain outside model requests. This prompt clarification does not
guarantee model compliance; both human confirmations are still required.

The ordinary runtime performs:
`model -> structured call -> ToolRegistry -> argument validation -> PermissionPolicy -> ToolExecutor -> isolated Windows service -> tool result -> final model response`.
This config exposes only the existing read-only foreground tool. The launcher
performs no application launch or keyboard/mouse operation.

Each receipt separates:

- Closed `desktop_tool_calls` metadata: fixed desktop tool name, `{}` for actual
  zero arguments (`null` for invalid/nonempty arguments), runner status, and
  whether an executor return and successful execution were recorded. Call IDs,
  arbitrary arguments, and unknown names are omitted. A missing executor return
  never proves that an external effect did not occur.
- `desktop_observations`: verified tool results, including truthful unknown
  status and bounded reasons. Generated prose cannot populate these records.
- The model's final reply and conversation/error status.
- Separate operator confirmation of the foreground observation and the answer.

A single-case pass requires one successful zero-argument `get_foreground_app`
call, an actual native result matching the expected case, a successful final
response, and both human confirmations. A model-only answer, denied call,
duplicate call, mismatched app, timeout after observation, or missing answer
cannot pass. Receipts include the commit; by default
they are saved under `.agent_workspace/desktop-demo`. `-EvidenceDirectory` can
select the operator's local evidence folder.

Notepad launch approval remains a separate interactive demonstration. JSON
chat cannot implicitly authorize it. The existing exact-tool approval gate,
denial behavior, cancellation, and unknown-launch duplicate suppression are
unchanged. No live conversation pass is claimed until the operator confirms
the real Windows result and final answer. No cloud setup or billing is needed
for this NVIDIA profile.

## Notepad denial before any approval test

Use a separate operator-local copy of the working model profile. Keep CONFIRM,
no capability overrides, one tool round/call, and set `tools.enabled` to exactly
`["open_notepad"]`. The foreground-only profile remains unchanged. From a real
PowerShell terminal with the existing provider key, run the denial test once:

```powershell
& .\tools\Start-Desktop-Demo.ps1 -Config .\agent.notepad-denial.local.toml -NotepadDenial
```

This sends `Jarvis, open Notepad.` through ordinary conversation. At the genuine
`[Desktop approval]` prompt, type **no**, then Enter. Do not enter the approval
phrase. The launcher inherits console stdin and does not use `--json`, pipe
answers, or supply an approval decision. The existing console gate and executor
remain the only approval/execution path.

`--desktop-evidence FILE` saves closed evidence alongside the normal human
output. It requires this restricted interactive profile and creates a new file
before sending the conversation turn; an existing file is never overwritten by
the CLI. It records the real gate's request count, categorical decision and
whether an explicit `no` was entered, never the raw input. It separates the
model's zero-argument call, executor permission denial, native dispatch state,
native outcome if present, and final model reply. Missing executor evidence
retains unknown dispatch status. A receipt-write failure must never trigger
an automatic repeat.

A denial pass requires one validated `open_notepad({})` request, exactly one
real approval request and explicit denial, returned executor `permission_denied`,
positive evidence of no native dispatch, an accurate final answer, and separate
operator confirmation that no new Notepad window appeared. Existing windows
are not evidence of a launch. Model refusal, an absent/disabled tool, redirected
stdin, policy denial without a real prompt, EOF/cancellation, and unknown
outcomes cannot pass this test. Stop after saving the denial receipt. Prepare
an approval demonstration only after that receipt is operator-confirmed and
independently reviewed; this mode offers no automatic second test.
