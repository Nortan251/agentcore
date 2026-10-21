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
and the model does not invent an app identity.

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
