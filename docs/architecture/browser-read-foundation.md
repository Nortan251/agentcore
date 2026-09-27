# Browser Read v1: brokered navigation and offline rendering

Base: `72c6ee3d2c3530299c04e5088008c5a2caacc327`.
This foundation supports **brokered read-only web navigation / offline rendering**.
Direct networked Chromium navigation remains disabled. Browser access requires
explicit host enablement and installation of the optional browser extra/binary.

## Authority and production composition

```text
BrowserAgentComposition / BrowserConversation
 -> fixed scoped ToolRegistry -> validation -> canonical ToolExecutor
 -> PermissionPolicy / NET_READ -> BrowserReadTool -> BrowserSession
 -> exact host-owned per-turn navigation grant check
 -> SafeFetchBroker: canonicalize -> resolve/check all IPs -> numeric socket connect
 -> inert HTML projection -> owned offline Playwright worker -> Chromium
 -> bounded UNTRUSTED_DATA title/text/links -> Role.TOOL conversation history
```

The reviewed catalogue contains exactly `browser_open_url`,
`browser_current_page`, `browser_extract_text`, and `browser_list_links`.
Their immutable specs declare `read_only=True` and exactly `{NET_READ}`.
No second executor, browser agent/model stack, action adapter, credential source,
permission authority, or filesystem authority is introduced. Gmail is unchanged.

The production entry point is a dedicated composition, not tool registration
into an unrestricted ordinary Agent:

```python
from agentcore.browser import BrowserAgentComposition, BrowserConfig

with BrowserAgentComposition(model=model, config=BrowserConfig(enabled=True)) as browser:
    conversation = browser.conversation()
    reply = conversation.send(
        "Read the approved page",
        navigation_urls=("https://example.com/",),  # trusted host input
    )
```

`navigation_urls` is a deliberate host API, separate from the natural-language
message and every model tool argument. The host supplies at most 16 complete
HTTP(S) URLs for one synchronous turn. `canonical_url()` validates and stores
only their full canonical network form: scheme, host, path and query; fragments
are irrelevant. A model proposal must match an exact grant before backend
startup, DNS, broker worker creation, socket connection or Chromium rendering.
Changing the path or query, including appending conversation data, fails with
`browser_navigation_not_authorized` without reflecting the URL. Canonical
equivalents may match, and the exact target may be reused within that one turn.
The normal ToolExecutor still evaluates NET_READ before calling the browser
tool; the host grant is an additional browser-specific check before network I/O.

Grants are in-memory composition state, never ConversationSession metadata,
Durable Memory, AgentState, ActionJournal, credentials or generic logs. They
are revoked at turn completion, exception and composition close. A new process
or persisted session resume starts with no navigation authority. The host must
explicitly reauthorize any URL on a later turn, including a URL suggested by
`browser_list_links`. Page text and links are UNTRUSTED DATA; the model cannot
promote them to network authority or use a private-data-dependent choice among
page-provided links to create a covert request. The model may instead ask the
host/user to authorize an exact discovered URL on a later turn. The hosting
layer must not automatically scrape URLs from messages or transcript history.
Server `Location` redirects remain part of the approved initial fetch and
continue through the broker's existing per-hop SSRF checks; they do not require
another model navigation grant.

Defaults perform zero browser/provider/DNS I/O and advertise no tools. A closed
or unavailable backend, or any missing/replaced original registration, produces
an empty isolated catalogue. There is no ordinary Agent fallback. Dependency or
binary launch failures are categorical and close the browser session; host
installation is separate from tool execution.

The scoped runner acquires execution leases on all four exact original bindings
for catalogue generation and dispatch, and calls the existing ConversationToolRunner
and exact ToolExecutor. Post-registered ordinary tools and same-name replacements
cannot enter. Injected alternate runners/tool catalogues are rejected; conversation
composition fields are sealed. Host-private test seams are not model parameters.
Ordinary non-browser Agent behavior remains unchanged.

BrowserReadTool implements the trusted host-owned `ExternalReadTool` contract.
It adds no permission capability, credential exception, approval, or journal
mutation classification. After checking the captured policy, the canonical
ToolExecutor atomically validates that exact policy identity and generation
before leasing it through synchronous Tool.run, including broker worker GET,
redirects, offline rendering and cleanup. Completed replacement, including ABA,
refuses the read before I/O. Concurrent replacement during the lease returns
`PolicyLeaseBusyError` and leaves the policy unchanged; hosts may retry replacement
after the bounded invocation exits. No metadata mutex spans I/O, and every exit
releases the lease. Gmail's existing credential leases and busy behavior remain
separate and unchanged. Privacy tags never select this authority mechanism.

ModelRequest keeps the neutral ToolSpec catalogue. OpenAI-compatible providers
translate it to function tools with object JSON Schemas at the provider boundary,
omitting capability, read_only, version and tag metadata. Unknown arguments remain
forbidden (`additionalProperties: false`). Unsupported or malformed parameter
contracts fail before provider I/O. Representable ASCII patterns use full-match
anchoring; Python-specific regex features are refused. Other providers retain
their neutral request contract and local validation remains authoritative.

An optional host-selected session directory must be exclusive to browser sessions.
The browser-specific transcript retains bounded USER/ASSISTANT/TOOL history on
resume, never persisted SYSTEM history. A browser marker checks session routing;
it grants no execution authority. Resumed sessions bind only to the current
reviewed composition and exact registrations. Hosts must not deliberately replay
browser transcripts into an unrestricted Agent. Models cannot choose stores,
compositions, directories, or resume authority.

During one tool round, the neutral ModelRequest carries the assistant's exact
structured calls followed by matching tool-call IDs. The OpenAI-compatible
provider translates and validates that sequence before HTTP I/O. These protocol
fields, including raw model arguments and call IDs, remain in memory for that
turn and are not written through ConversationSession's persisted message shape. A later
browser turn replays bounded historical TOOL content only as explicitly labelled
untrusted data at user level; without the original envelope it is never sent
as a provider `role: tool` message. Current-turn tool results retain their
normal tool protocol role and still pass through ToolExecutor.

## Safe-fetch connection boundary

The address validated by AgentCore is the address actually dialed. `fetch.py`
retains canonical IP strings from one complete A/AAAA resolution per hop. Every
answer must be public; mixed public/private answers fail closed. The connector
creates AF_INET/AF_INET6 SOCK_STREAM sockets and passes an exact retained numeric
sockaddr to `socket.connect`. It does not call `create_connection`, a hostname
HTTP connector, a proxy, a connection pool, or another resolver. The connected
peer address is also checked. One validated answer is selected, with no hidden
retry or fallback. Redirects repeat canonicalize/resolve/check/pin from scratch.

The conservative URL policy allows ASCII HTTP(S) and their default ports only.
It rejects credentials, encoded/ambiguous authority, backslashes, controls,
noncanonical numeric hosts, single-label/internal/localhost names, and forbidden
IPv4/IPv6 ranges. IPv4-mapped, translation, 6to4, Teredo and site-local forms are
rejected. IPv6 is conservatively limited to ordinary global unicast ranges;
reserved/special-use subranges are refused. No model headers, proxies, cookies,
authorization, executable paths or launch flags exist. Environment proxies are
irrelevant to numeric raw sockets. The host OS routing/resolver/trust store remain
trusted; a compromised host or malicious network redirecting numeric public
addresses is outside this application boundary.

TLS wraps that already-connected socket with SNI and certificate hostname
verification set to the original canonical hostname. CERT_REQUIRED,
check_hostname=True, TLS >=1.2 and the host trust store are mandatory. An explicit
SSLContext avoids inheriting SSLKEYLOGFILE; certificate verification is never
disabled. Fixed Host preserves the canonical original hostname (brackets for IPv6).

Only GET/HEAD are possible. Headers are fixed by the host, including
Accept-Encoding: identity and Connection: close. Redirect following is manual,
at most three hops; HTTPS-to-HTTP downgrade is rejected. No response cookies or
authentication are forwarded. Only status 200 HTML/plain text is rendered; redirect
statuses only supply a bounded next URL. Header/status parsing is strict and
bounded to 32 KiB, 100 fields and 8 KiB lines. Ambiguous framing, folded/duplicate
fields, malformed chunks and trailers are rejected. Set-Cookie values are discarded.
Body bytes are capped at 256 KiB by default for content-length, chunked and EOF
framing. Non-identity compression is rejected entirely. Supported charsets are
UTF-8, ASCII, ISO-8859-1 and Windows-1252; plain text is escaped into inert HTML.
Redirect bodies are not downloaded or rendered.

Each fetch uses an owned spawn worker. A hard parent deadline bounds the complete
operation, including connect/TLS/read/redirect processing; each resolver stage
also has a separate parent-owned deadline. A blocked system getaddrinfo is killed
with its worker. IPC readers cannot block the parent on partial frames. Failure
objects contain categories only, never request URLs, queries, headers, addresses,
OS errors or raw provider exceptions. Cleanup uncertainty overrides apparent success.
`BrowserNetworkPolicy` remains a supplementary screening interface for existing
foundation tests; it is not used as a Chromium connection authority.

## Offline renderer boundary

Production never calls public `page.goto`, route.continue_ or route.fetch.
Fetched HTML first passes through an original allowlist projection into inert
structural markup. Scripts/styles/forms/editable content/frames/plugins/SVG/math,
base elements, meta refresh, active attributes and external resources are removed.
Only parent-canonicalized HTTP(S) anchor destinations survive as escaped
`data-agentcore-href` values, not navigable hrefs. They resolve against the
broker's final fetched URL; hostile base elements have no authority. This avoids
exposing resource/file navigation markup to Chromium;
offline HTTP routing alone would not be a filesystem boundary.

Playwright renders that bounded projection with `page.set_content`. The owned
fresh non-persistent context has JavaScript disabled, service workers blocked,
offline=True, all HTTP routes aborted and websocket routes closed. Downloads
are off; popups close and dialogs dismiss. No profile reuse, CDP attachment,
extensions, credentials, clipboard, traces, HAR, videos or screenshots are used.
Fixed host adapter scripts extract only bounded main-document title/text/link
fields; arbitrary JavaScript is never exposed to the model.

The trusted final fetched URL is retained separately from Chromium's about:blank.
The renderer scans at most max_dom_nodes elements and deduplicates visible
canonical destinations before applying max_links, so unusable or repeated raw
anchors cannot consume the usable-result budget. IPC carries at most one extra
usable link to establish truncation. Destinations are bounded proposals, not approval; opening a link
requires another NET_READ tool invocation and complete pinned-fetch validation.
Stable first-document ordering/deduplication and deterministic text/link/node
caps apply. Explicit hidden/form/editable text is excluded.
Hidden unsupported wrappers and closed `dialog`/`details` subtrees are suppressed;
closed `details` summaries are conservatively omitted too. Native fallback
subtrees in audio, video, datalist, progress, meter, ruby `rp`, noembed,
noframes and slot are conservatively omitted, while canvas fallback text and
ruby `rt` remain readable. Boolean `open` is true by presence, even when its
value is `false`.
Any non-void element with a `popover` attribute is conservatively suppressed
regardless of its value: offline Browser Read cannot establish an open runtime
popover state after stripping active attributes.

This is static readable HTML, not full browser fidelity. No CSS, fonts, images,
frames or other subresources are fetched. Class-based stylesheet visibility,
complex layouts and JavaScript-heavy sites are unsupported; no compatibility
exception weakens the boundary. Inline explicit hiding is preserved conservatively.
Only display, visibility and opacity declarations are interpreted: comments,
whitespace, case, important suffixes and numeric-zero opacity forms are normalized.
Any hidden or ambiguous relevant declaration hides, even if later overridden;
this intentionally does not reproduce the CSS cascade. Original styles are
discarded; the only generated style is `display:none`. Stylesheet/class-driven
visibility is not reproduced.
CSS escapes are not decoded: any escaped inline declaration or disallowed
control hides the element conservatively, including unrelated escaped properties.
Every duplicate style and aria-hidden attribute is examined before dictionary
conversion; any explicit hidden signal wins. This can hide otherwise visible
content and does not reproduce browser attribute-conflict semantics.
At the provider boundary, emitted choices/defaults must recursively contain only
JSON-native lists, string-keyed dictionaries and finite scalar values. Tuples,
cycles, non-string keys and coercible non-native values fail before provider I/O;
host ToolParameter values and local validation are unchanged.
No interactive action or direct Chromium networking is ready for enablement.

## Lifecycle and accepted containment limitation

BrowserSession and workers have one process/thread owner. Startup is lazy and
at most once; closed sessions are terminal. Normal close is idempotent. Failed
cleanup remains categorical on repeated close. Exceptions, cancellation and
interruptions close owned resources. Model requests have bounded operation
timeouts; process cleanup has separately bounded waits. Cancellation is not a
claim that a remote GET did not occur.

Playwright runs in an owned spawn worker with Chromium sandbox enabled. psutil
handles fence sampled descendants by PID/create-time. Normal shutdown requests
driver/browser/context close; the parent kills remaining observed descendants,
waits for their exit and independently joins/kills its owned worker. Stalled large
IPC writes are interrupted by terminating the worker. Enumeration/access/exit
or IPC-thread cleanup uncertainty yields browser_cleanup_unverified, not success.
The fetch worker is also terminated/killed/joined on deadlines and exceptions.

Sampled process-tree cleanup cannot establish perfect containment of children
that reparent between observations. Windows Job Objects/Linux cgroups are not
implemented. The project lead accepts this limitation for offline-render-only
v1; complete containment is required before interactive/direct-network browsing.
Successful cleanup verifies the observed owned tree, not every possible process.
Playwright may create managed temporary browser artifacts internally; there is
no host-profile reuse or application storage-state persistence. Crash residue
and perfect deletion are not certified.

## Privacy, persistence and read/write classification

Page bodies, titles, link text and link URLs are hostile UNTRUSTED_DATA. Metadata
exposes origin only, omitting path/query/fragment. Link URLs may contain queries
as bounded untrusted tool data, never operational evidence. Worker diagnostics
are suppressed; browser debug/proxy/TLS-keylog environment settings are removed.
No browser content enters Durable Memory, ActionJournal, credentials, generic
AgentState or logs. Browser composition has no memory/capture/resolver/state or
autonomous run/checkpoint path. Page HTML is ephemeral broker/renderer input.

Bounded Role.TOOL results intentionally can persist in ConversationSession when
a host selects a file session store. The transcript can therefore contain page
text/links. Isolation plus untrusted framing is the boundary; absence of content
from the transcript is not claimed. In-memory sessions are the default.

The trusted immutable ToolSpec `private_arguments` tag affects only argument
redaction and categorical error/log observability. Constructor tag inputs are
detached into tuples. Model arguments cannot modify tags. It does not grant
capabilities, permissions, approvals, authenticated authority, action eligibility,
mutation classification, execution leases or success. Verifier/recovery input
continues to use original trusted task arguments, not redacted invocation evidence.
Raised framework failures retain their trusted retryability and framework category;
raw messages/details and arbitrary provider class names are omitted.
Generic regressions compare marked/unmarked read and mutation paths at every
permission level and preserve Gmail's existing exact authenticated-read behavior.

Browser navigation changes only an ephemeral document pointer, so these tools
are NET_READ and produce no mutation ActionRecord. GET cannot guarantee that a
remote server itself has no side effects. Future clicks/types/forms/account
changes/uploads require separately reviewed NET_WRITE mutation adapters,
approvals, journal/reconciliation semantics. Downloads also require filesystem
authority. No future mutation permission is granted by this phase.

## OSS inspection, reuse and licenses

| Repository | Exact inspected commit | Sources / role |
| --- | --- | --- |
| microsoft/playwright-python | `0e66a0927e5431b0f659ff41b6544de96b0486f5` | `_impl/_browser.py`, `sync_api/_generated.py`; mature runtime dependency/API |
| browser-use/browser-use | `50f205533fe10ba35b553d2a3689c77b87bd5d0a` | `browser/session.py`, `views.py`, local/crash watchdogs; lifecycle/process/state patterns |
| browserbase/stagehand | `b771930d2b4d858e5bd9670203c66260b385a8fa` | observe/extract examples and object-wrapper tests; separation inspiration |
| ServiceNow/BrowserGym | `9e779f087de9a65668b6974d11f9ce9816026e96` | core observation.py; evaluation/visibility inspiration only |
| gradio-app/safehttpx | `e1e0586820f396bee226f67c1532b76edacad9f6` | `safehttpx/__init__.py`, README, LICENSE; SSRF design/audit provenance |
| felixchaos/safe-outbound | `f9131b1e9b5f811c0d0c8773a8a773abb1c4d6b4` | `src/safe_outbound/client.py`, README/LICENSE; resolve/pin/Host/TLS/redirect reference |
| tommypj/ssrf-safe-fetch | `56578ac1999723514bedc0f78be16b244fd71163` | fetch.py, resolver.py, README/LICENSE; per-redirect pinning reference |

Playwright, BrowserGym and safehttpx are Apache-2.0; psutil is BSD-3-Clause. browser-use, Stagehand,
safe-outbound and ssrf-safe-fetch are MIT. No substantive external source was
copied, translated or adapted; all AgentCore adapters/scripts are original.
No vendored third-party notices are required for these original files. Only
Playwright and psutil are optional runtime dependencies; their distributed
licenses/notices, including Chromium notices, must accompany redistributed
packages/binaries. No young security-library dependency, cloud/browser-agent
stack, BrowserGym runtime, Skyvern or AGPL code is added.

safehttpx's README links the 2024 Trail of Bits Gradio audit as design provenance;
that provenance does not audit this inspected version or AgentCore. Its observed
first-public-answer selection/bypass seams and other references' generic request,
redirect/decompression/environment surfaces were not adopted. The small original
broker uses mature Python socket/ssl/multiprocessing primitives instead.

## Installation and verification

```shell
python -m pip install -e ".[dev,browser]"
python -m playwright install --with-deps chromium
```

The extra pins Playwright 1.62.0 and psutil 7.2.2; Playwright selects its Chromium
revision. Installation/upgrades are host operations and require real gate review.
Core/dev installs and the ordinary suite do not require Chromium.

Linux hosts must support Chromium's sandbox user namespaces. On Ubuntu with
restricted unprivileged namespaces, downloaded Chromium needs a host-installed
AppArmor allowance for its exact binary paths, as described in the
[Chromium sandbox documentation](https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md).
The Linux browser CI installs that narrowly scoped allowance for its downloaded
binaries, preserves the global restriction, and runs all real tests with
chromium_sandbox=True. A blank host probe reports only categorical prerequisites.
Tools never install profiles or disable the sandbox to recover from launch failure.

Set AGENTCORE_BROWSER_INTEGRATION=1 to enable real browser tests. Custom binary
installs require a matching PLAYWRIGHT_BROWSERS_PATH. Enabled gate failures are
failures, not dependency skips. Controlled HTTP/TLS fixtures use a test-defined
private resolver/connector seam, never a production allow-localhost flag. Tests
prove production loopback rejection independently, original Host/TLS SNI and
certificate verification, exact dial addresses, rebinding resistance, bounded
responses/timeouts and categorical failures. Real fetch/render tests require
only local fixtures and prove that only broker GETs hit the server, JS/resources
never execute/fetch, relative links ignore hostile base, and persisted sentinel
injection cannot advertise or execute ordinary/replaced tools.

The browser workflow covers Linux and Windows with real Chromium; existing full
Linux/Windows workflows are unchanged. CI configuration is not evidence of green
CI; final exact-head results are recorded in the implementation report.
