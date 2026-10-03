# Browser Read v1.1 static capacity

Authoritative base: `dfedf634e3a2bbbf4f77e5fc03630446bb982069`.
This changes bounded, complete static HTTP 200 input capacity. It follows the
[pre-v1.1 compatibility baseline](browser-read-v1.1-compatibility-baseline.md);
it adds no interactive browsing capability or authority.

## Host-owned input limits

| Bound | Pre-v1.1 default | v1.1 default | Host configuration ceiling |
| --- | ---: | ---: | ---: |
| Individual response header/status line, including CRLF | 8,192 bytes | 24,576 bytes | 24,576 bytes |
| Complete fetched body | 262,144 bytes | 2,097,152 bytes | 2,097,152 bytes |
| Derived renderer source, UTF-8 bytes and characters | 1,572,880 | 12,582,928 | `max_body_bytes * 6 + 16` |

The appended `BrowserConfig.max_header_line_bytes` field preserves existing
positional constructor fields. Both input limits require exact integers
(booleans excluded), at least one and no greater than their ceilings. Hosts
can select lower limits. Neither bound appears in CLI/model tool arguments;
unknown model arguments fail validation before browser I/O.

The characterized Wikipedia-like fixture has a 20,000-byte CSP line and
1,782,579-byte HTML body. A finite 24 KiB line budget comfortably accepts that
line. A 2 MiB body budget is the smallest conventional power of two above that
body. All header names follow the same rule; there is no CSP exception.
The independent 32 KiB aggregate header ceiling can still reject a response
whose individual lines fit.

## Independent bounds and complete framing

Aggregate response headers remain 32,768 bytes, including the status line and
terminating blank CRLF. At most 100 fields are accepted. Strict CRLF, ASCII,
token, duplicate/folded field and status checks remain. Chunk-size lines retain
their 32-byte budget; trailer termination retains 8,192 bytes.

All three body modes use the same host cap. Oversized Content-Length rejects
before any body read. Chunk accumulation rejects the offending size before
reading that chunk's payload. EOF framing reads at most cap + 1 and rejects
oversize. Accepted bodies are complete. No HTML truncation, reconstruction,
fake closing tags or partial success is introduced.

The source limit remains derived: `max_body_bytes * 6 + 16`. Plaintext is
escaped with `html.escape` inside `<pre></pre>`. The worst ASCII escaping
expands each apostrophe to six characters; at the body cap this produces
12,582,923 UTF-8 bytes, five below the source ceiling. Broker parent and
renderer parent/worker checks still enforce the derived limit, including
multibyte UTF-8 byte accounting, independently of raw fetched bytes.

Unchanged defaults are 10,000 DOM nodes, 8,000 text characters, 50 links,
256 title/link-label characters, 2,048 URL characters, three redirects,
15-second operations and five-second DNS timeout. Larger input does not enlarge
model-visible projections. All outputs remain `UNTRUSTED_DATA`.

## Regression and resource evidence

The historical response corpus uses explicit 8,192-byte header lines and
262,144-byte bodies. Separate v1.1 cases test N-1/N/N+1 for header/status lines,
aggregate headers, field count, all three body framing modes and lower host
settings, with body-read observations. Source checks cover ASCII and multibyte
N-1/N/N+1 through the broker and renderer parent, plus direct worker rejection.

The opt-in real Chromium gate fetches the unchanged synthetic Wikipedia-like
and Python-like fixtures through production SafeFetch parsing, then renders
their complete source offline. Text and links remain bounded with truthful
truncation flags. Complete 2 MiB - 1 and 2 MiB low-node HTML documents exercise
the same path. Small-byte documents test the unchanged DOM N-1/N/N+1 guard.
One real renderer case accepts the worst escaped 2 MiB plaintext source under
the unchanged timeout. A separate complete 2 MiB document includes attempted
script, image, frame, stylesheet, font, media and WebSocket activity; a local
server records no requests and script mutation stays inert. Each new renderer
fixture owns and closes one backend per test and verifies owned process death.
Enable with `AGENTCORE_BROWSER_INTEGRATION=1`; skipped cases are not renderer
evidence. No public sites or scraped fixtures are used.

## Remaining incompatibilities and security boundaries

Status 202 and other non-200 renderable responses remain rejected. gzip,
deflate, br and unknown content encodings remain rejected; requests still use
`Accept-Encoding: identity`. Unsupported content types/charsets, malformed or
conflicting framing, input beyond the finite bounds and excessive DOM counts
remain incompatible. JavaScript-dependent pages still yield only static or
fallback content; clicks, forms, login and dynamic navigation remain absent.

SafeFetchBroker remains the sole network authority, with all-answer DNS
validation, retained numeric IP dialing, connected-peer verification, original
hostname TLS verification, TLS >= 1.2 and per-hop redirect validation. Host
navigation grants, tool isolation, exact `{NET_READ}`, GET/HEAD-only behavior,
HTTPS downgrade rejection and absence of proxies, cookies or authorization
remain unchanged. Chromium remains offline, JavaScript disabled, service
workers blocked, routes restricted to the synthetic document and WebSockets
closed in fresh nonpersistent contexts. Process ownership and categorical
public errors are unchanged. No URLs, headers, body bytes, IPs or OS errors
are added to failure disclosures.
