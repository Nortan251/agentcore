# Browser Read renderer boundary rework (stacked draft)

This stacked change replaces Python's hand-written visibility projection. The
frozen Browser Read branch remains the base. The SafeFetchBroker alone validates,
resolves, dials, and bounds web responses; Chromium remains an offline renderer.

The renderer receives the bounded fetched HTML without markup rewriting. In a
fresh non-persistent Chromium context, a route registered before page creation
fulfills one GET main-frame navigation to a fixed host-owned synthetic HTTPS
URL. It aborts every later request, including repeat navigation to that URL.
The handler never continues or fetches a request. The fetched final URL remains
separate from Chromium's synthetic document URL and is the only authority for
link canonicalization and operational page identity.

The synthetic response sets a restrictive CSP: `default-src 'none';
script-src 'none'; connect-src 'none'; img-src 'none'; media-src 'none';
font-src 'none'; object-src 'none'; frame-src 'none'; child-src 'none';
worker-src 'none'; form-action 'none'; base-uri 'none';
style-src 'unsafe-inline'; sandbox`. JavaScript stays disabled independently;
service workers stay blocked, WebSockets closed, downloads disabled, and popups
and dialogs closed. Inline style remains available for Chromium's native
visibility decisions. No external stylesheet or subresource is fetched.

The opt-in Chromium proof demonstrates that one local `route.fulfill` succeeds
with `offline=True` and a second navigation is aborted. No fixture network
request occurs. Temporary local-file probes through iframe, object, embed,
image, stylesheet, `@import`, and CSS background do not reveal the sentinel or
apply local CSS. Data/blob/JavaScript frame proposals do not load active
documents. Meta refresh does not change the document URL. Inline CSS and
Chromium's native frameset parsing preserve hidden text, and raw link attributes
remain relative despite a hostile `<base>`. Blocked iframes may leave inert
blank/error-frame placeholders; the requested file or network document is not
loaded. These are renderer controls, not an independent OS filesystem sandbox.

The production path removes `static_html()` reconstruction, retains broker
byte and operation deadlines, applies a bounded post-parse DOM walk, and
extracts bounded title/text/link data from Chromium's original parsed document.
`max_dom_nodes` limits the parsed DOM walk, not the CPU cost of parsing the
already broker-bounded body. Page text is the root Chromium `innerText`, sliced
to `max_text_chars` with truncation determined before slicing. Link discovery
uses Playwright 1.62.0's default non-hidden `link` role. Each bounded batch of
at most 64 role candidates retains only actual `HTMLAnchorElement` instances
with an explicit `href`; it returns the raw attribute and bounded anchor
`innerText` label. The trusted parent resolves, validates, filters, and
deduplicates raw hrefs against the broker's final URL before the usable-link
limit applies. Accessibility semantics may omit visually present links, such
as `aria-hidden` anchors or anchors overridden to `role=button`. Empty,
image-only, and ARIA-label-only anchors can have empty rendered labels.

These are observational projections, not a pixel-visibility security filter.
Opacity-zero content, slot fallback, form/editable text, or direct inline
`content-visibility:hidden` text can appear if Chromium includes it. Extraction
never changes the DOM, style, or attributes; hostile CSS could react to such
changes. All returned content remains `UNTRUSTED_DATA`. It cannot grant URL
navigation or ordinary tools: BrowserAgentComposition isolates the catalogue
and exact trusted host grants separately authorize each model-proposed URL.
The model receives neither arbitrary JavaScript execution nor the synthetic
URL. JavaScript-heavy pages and external CSS remain unsupported.
