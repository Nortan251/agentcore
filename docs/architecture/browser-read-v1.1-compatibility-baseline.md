# Browser Read v1.1 compatibility baseline

Authoritative base: `f4e544b52d0f4676ecb96903efee8699d8ce847b`.
This is characterization, not a production compatibility change. Synthetic
fixtures model response classes; they are not scraped pages or claims about
current public sites. All limits below record the pre-v1.1 default configuration;
the characterization corpus retains explicit legacy header/body settings.
See [v1.1 static capacity](browser-read-v1.1-static-capacity.md) for the subsequent
bounded production capacity change. Measurements and conclusions below remain
historical evidence.

## Evidence and measurement

`tests/test_browser_response_compatibility.py` adds 129 deterministic cases.
It drives the production `_fetch_document -> _exchange -> _headers/_body`
path with in-memory wire bytes, a fake connector and a fake public resolver.
An autouse guard denies real socket creation and DNS. Generated-source tests
also exercise the real broker parent over spawned IPC, with network-free workers.

`tests/test_browser_native_visibility.py` adds 31 real offline Chromium cases
to the existing browser CI selection. These cover accepted response bodies,
static/fallback projections, DOM/source/output boundaries and cut HTML. Their
adapter uses the production offline context, original source, fixed synthetic
document route and restrictive CSP. Enable with
`AGENTCORE_BROWSER_INTEGRATION=1`; skips without that flag are not renderer evidence.
No public network, scraped fixtures, new dependency, sleeps or timing-based
synchronization are added. Existing suites retain their own local-server and
deadline fixtures.

Test-only failure stages assert the actual innermost production raise site,
identified by function and guard rather than a hard-coded line number. The
shared `_line` rejection is disambiguated by its live remaining byte budget.
Body-read observations distinguish declared-length rejection before any body
read, chunked rejection after the size line, and EOF rejection after reading
at most cap + 1. No runtime instrumentation or new public error fields exist.

Public codes in the matrix are abbreviated:

- `RESPONSE`: `browser_response_rejected`.
- `SIZE`: `browser_response_too_large`.
- `REDIRECT`: `browser_redirect_rejected`.
- `DESTINATION`: `browser_destination_denied`.
- `FETCH`: `browser_fetch_failed`.
- **Accepted** means fetched/decoded source, with real rendering proved where
  indicated. Redirect acceptance includes fetching a final synthetic 200.

## Compatibility matrix

| Case | Current result | Limiting stage | Security reason | Candidate v1.1 direction |
| --- | --- | --- | --- | --- |
| 200 HTML | Accepted | None | Bounded supported document | KEEP AS-IS |
| 202, 204, 206 | RESPONSE for each; no body read | Status policy | Interim/no-content/partial responses have different semantics | NEEDS ARCHITECTURE DECISION |
| 301, 302, 303, 307, 308 | Each follows validated Location to 200 | Per-hop redirect validation | No automatic authority bypass or body replay | KEEP AS-IS |
| 400, 404, 429, 500 | RESPONSE for each; no body read | Status policy | Error pages are not successful navigation | KEEP AS-IS |
| CSP, Permissions-Policy, Link, Report-To, Reporting-Endpoints lines of 8,191 / 8,192 bytes | Accepted for every header name | None | Bounded parser input | KEEP AS-IS pending measured capacity decision |
| Each of those lines at 8,193 / 20,000 bytes | RESPONSE; no body read | Individual header line | Bound parser memory/work; no header-name exemption | MAY SAFELY RAISE WITH BOUND, subject to resource review |
| Aggregate headers 32,767 / 32,768 bytes | Accepted | None | Aggregate includes status line and final blank CRLF | KEEP AS-IS |
| Aggregate 32,769 / 65,536 bytes, each line below 8 KiB | RESPONSE; no body read | Aggregate header budget | Independently bounds cumulative work | KEEP AS-IS for this corpus; no evidence it needs raising |
| 99 / 100 fields | Accepted | None | Count includes Content-Type and Content-Length | KEEP AS-IS |
| 101 fields | RESPONSE; no body read | Header count | Bounds many-small-header work | KEEP AS-IS |
| HTML 64 / 128 / 255 KiB; 262,143 / 262,144 bytes, all three framings | Accepted; corresponding length-framed bodies also render | None; text projection truncates | Bounded fetch and independent output | KEEP AS-IS or measured bounded static-input capacity review |
| HTML 262,145 / 524,288 / 1,048,576 / 1,782,579 bytes with Content-Length | SIZE; no body read | Declared body size | Reject excessive allocation before read | MAY SAFELY RAISE WITH BOUND, subject to resource review |
| Same oversized bodies, chunked | SIZE after chunk size, before chunk payload | Framing/body accumulation | Decoded transfer payload must remain bounded | Preserve finite cap and strict framing |
| Same oversized bodies, EOF | SIZE after cap + 1 bytes | Body read | Unannounced size must remain bounded | Preserve finite cap |
| Valid Content-Length / chunked / EOF; zero body in each | Accepted | None | Only supported unambiguous framing | KEEP AS-IS |
| Conflicting / identical repeated Content-Length | RESPONSE; no body read | Duplicate header | Avoid ambiguous message interpretation | SHOULD REMAIN REJECTED |
| Transfer-Encoding + Content-Length | RESPONSE; no body read | Framing conflict | Avoid competing length authorities | SHOULD REMAIN REJECTED |
| Malformed chunk size / truncated chunk or length / bad chunk terminator / trailer field | RESPONSE at exact syntax/read/trailer guard | Response framing | Never silently accept malformed or incomplete transport | SHOULD REMAIN REJECTED |
| Folded header / invalid HTTP status line | RESPONSE | Header syntax / status grammar | Strict parser interpretation | SHOULD REMAIN REJECTED |
| identity | Accepted | None | No decompression expansion | KEEP AS-IS |
| gzip / deflate / br / unknown | RESPONSE for each; no body read | Content encoding | No uncontrolled decompression | SHOULD REMAIN REJECTED in the smallest scope |
| HTML UTF-8 / plain UTF-8 / us-ascii / ISO-8859-1 / Windows-1252 / missing charset | Accepted; plain text escaped in pre | None | Small explicit codec/media allowlist; missing charset defaults to UTF-8 | KEEP AS-IS |
| Unsupported charset | RESPONSE; no body read | Charset allowlist | Avoid unreviewed decoding behavior | SHOULD REMAIN REJECTED |
| application/json / application/octet-stream / malformed or missing Content-Type | RESPONSE; no body read | Content-type grammar | Only supported document formats reach renderer | KEEP AS-IS |
| Invalid byte in declared UTF-8 HTML | Accepted with replacement character | Decode, no rejection | Existing errors=replace semantics, bounded source | KEEP AS-IS; do not claim strict decoding |
| Relative / absolute / fragment-only / query Location | Accepted | None | Canonicalize and validate every hop | KEEP AS-IS; fragment-only still consumes a hop/refetch |
| 2 / 3 redirects | Accepted after 3 / 4 exchanges | None | Default redirect cap is three | KEEP AS-IS |
| 4 redirects | REDIRECT on fourth exchange; no fifth dial | Redirect count | Bound chains and repeated resolution | KEEP AS-IS |
| Missing Location | REDIRECT; no next dial | Redirect policy | No inferred destination | SHOULD REMAIN REJECTED |
| Malformed bracketed Location | FETCH at worker boundary; no next dial | urljoin parsing, sanitized by worker | Internal ValueError never crosses browser boundary | KEEP AS-IS; category refinement only if later needed |
| HTTPS to HTTP | REDIRECT before next resolution/dial | Downgrade check | Preserve transport security | SHOULD REMAIN REJECTED |
| Public to private DNS target / localhost / forbidden numeric address | DESTINATION before next dial | DNS answer validation / canonical URL | Preserve SSRF boundary | SHOULD REMAIN REJECTED |
| Generated source 1,572,879 / 1,572,880 UTF-8 bytes, ASCII and multibyte | Broker parent and renderer accept; real render projects bounded text | None | Derived source limit = body cap × 6 + 16 | KEEP AS-IS unless body budget changes coherently |
| Generated source 1,572,881 UTF-8 bytes | SIZE | Broker parent / renderer source validation | Bound escaped/decoded source independently | Preserve coherent derived bound |
| DOM 9,999 / 10,000 nodes | Real render accepted | None | Count includes document, elements and text nodes | KEEP AS-IS |
| DOM 10,001 nodes in sub-256-KiB inert HTML | SIZE after parse | DOM count | Byte count alone cannot bound structural work | KEEP AS-IS |
| Text 7,999 / 8,000 / 8,001 chars; links 49 / 50 / 51 | Accepted; N+1 truncates with flag | Output projection, no fetch failure | Bound hostile output before IPC | KEEP AS-IS |
| Title and link label 255 / 256 / 257 chars | Accepted; N+1 sliced to 256 | Output projection | Bounded metadata; no separate truncation flag | KEEP AS-IS |
| Wikipedia-like: 20,000-byte CSP line, 30 normal headers, 1,782,579-byte static HTML, 400 anchors | RESPONSE first | Header line | Early parser input bound | Review bounded line and body capacity together |
| Same large static body with ordinary headers | SIZE first | Declared body size | Independent payload cap | See bounded static-input scope below |
| Same large source sent directly to source guard | SIZE | Renderer source bound | Existing source cap is also below this fixture | Preserve a coherent source budget |
| Python-like: 64-KiB static HTML, 60 useful anchors | Real render accepted; 8,000 text chars / 50 links, flags set | Output projection only | Useful read-only bounded observation | KEEP AS-IS |
| JS-heavy search-like: small fallback and 80 inert scripts | Real render returns fallback only; no script sentinel or links | Content available without JS | Script/network confinement deliberately limits observations | KEEP AS-IS |
| HTTP 202 search-like valid HTML | RESPONSE before body | Status policy | Valid markup does not establish completed response semantics | NEEDS ARCHITECTURE DECISION |
| HTML cut inside tag or script raw text | Parses a different, incomplete DOM; readable suffix lost | Parser EOF semantics | A byte prefix does not preserve full-document interpretation | NEEDS ARCHITECTURE DECISION, not implicit truncation |

## Decisions supported by the corpus

1. **First Wikipedia-like blocker:** its 20,000-byte CSP wire line exceeds the
   8,192-byte line limit. `_headers/_line` rejects before `_body` executes.
   The limit counts the name, separator and CRLF, not just the policy value.
2. **Yes, the large header fails before body-size enforcement.** The corpus
   asserts zero body reads. This fixture's aggregate headers would fit within
   32 KiB, and its field count fits within 100; neither is its first blocker.
3. **If headers were accepted, the next blocker is the 262,144-byte body cap.**
   The ordinary-header variant proves declared-length rejection before body
   read. Independently feeding the same source to the unchanged renderer
   guard also fails its 1,572,880-byte bound. That is a further obstacle if
   body acceptance were changed without coherent source-budget handling.
4. **Capacity values versus security requirements:** exact byte/node/count
   values are compatibility choices, but finite independent resource bounds
   are security requirements. Strict framing, destination checks and renderer
   confinement are not compatibility budgets. The small-byte DOM overflow
   proves that increasing only body capacity cannot replace the DOM guard.
5. **202 needs a deliberate semantic policy.** Its HTML can be syntactically
   ordinary, but it describes an accepted/interim operation, not necessarily
   a final document. The corpus proves current early rejection, not that all
   2xx are interchangeable or that accepting 202 is obviously safe.
6. **Streaming and truncation are separate decisions.** Bounded streaming of
   a complete document may preserve parsing, but blindly cutting an HTML byte
   prefix can leave a tag or script raw-text section incomplete and loses later
   readable content. The real renderer tests show that changed observation.
   A future truncated-document feature needs explicit incomplete semantics,
   encoding handling and resource accounting; it cannot claim DOM equivalence.
7. **Almost certainly unchanged:** exact host-owned navigation grants;
   canonical executor/NET_READ authority; GET/HEAD only; all DNS answers checked;
   exact numeric dial and peer validation; original-host TLS/SNI/certificate
   verification; independent redirects and downgrade denial; no proxies,
   cookies or auth/model headers; strict framing and categorical failures;
   no compression in this scope; JavaScript disabled, offline Chromium,
   blocked subresources/service workers/popups/downloads/websockets; bounded
   UNTRUSTED_DATA output; no writes/forms/login or ActionJournal mutations.
   Keep the existing aggregate header, field-count, redirect, DOM and output
   caps unless separate evidence supports changing their values.
8. **Smallest coherent candidate production scope:** review only bounded
   static-200 input capacity: per-line header bytes and body bytes, together
   with configuration validation and the derived generated-source budget.
   The synthetic large fixture requires at least a 20,000-byte line and
   1,782,579-byte payload; these are fixture measurements, not recommended
   production maxima. Select actual budgets after memory/deadline review and
   realistic host-owned measurements. Preserve strict framing, aggregate
   budget, DOM/output caps and all network/renderer authority. Do not bundle
   202, generic 2xx, compression, JavaScript or input truncation into that
   change. No candidate direction is implemented here.

## Reproduction and CI coverage

```powershell
python -m pytest tests/test_browser_response_compatibility.py -ra
$env:AGENTCORE_BROWSER_INTEGRATION = '1'
python -m pytest tests/test_browser_native_visibility.py -k compatibility -ra
python -m pytest tests/test_browser_fetch.py tests/test_browser_fetch_integration.py -ra
python -m pytest tests/test_browser_session.py tests/test_browser_composition.py tests/test_browser_tool_boundary.py tests/test_browser_render_source_bound.py -ra
python -m pytest tests/test_browser_*.py tests/test_cli_browser.py -ra
python -m compileall -q src tests
git diff --check
```

For shells without wildcard expansion, enumerate `tests/test_browser_*.py`
using the shell's file-list mechanism before invoking pytest. Windows local
runs can add a checkout-local `--basetemp`.

The offline corpus is collected by the unchanged full Linux/Windows CI suites.
The 31 renderer cases are in the unchanged explicit Chromium Linux/Windows
workflow selection. No workflow/governance or Browser CLI v1 file is modified.
Exact local counts and exact-head CI results belong in the PR/handoff;
configured CI is not evidence of green CI.
