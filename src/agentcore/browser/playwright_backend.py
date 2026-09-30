"""Owned offline Chromium renderer with hard parent deadlines.

The renderer itself does not advertise network isolation or fetch URLs. Only
BrokeredBrowserBackend can pair it with AgentCore's pinned-IP SafeFetchBroker.
"""

import multiprocessing
import queue
import os
import threading
import time

from .backend import PageLink, PageLinks, PageMetadata, PageText
from .config import BrowserConfig
from .errors import BrowserError, BrowserFailure

# Fixed host-owned synthetic origin; never derived from a fetched URL or HTML.
_DOCUMENT_URL = "https://agentcore.invalid/document"
_CSP = ("default-src 'none'; script-src 'none'; connect-src 'none'; img-src 'none'; "
        "media-src 'none'; font-src 'none'; object-src 'none'; frame-src 'none'; "
        "child-src 'none'; worker-src 'none'; form-action 'none'; base-uri 'none'; "
        "style-src 'unsafe-inline'; sandbox")

# Fixed trusted adapter scripts, never model supplied. Bound results in
# Chromium before IPC. Main document only; no arbitrary DOM dump.
_METADATA = """limit => ({url: location.href.slice(0, 2049),
    title: document.title.slice(0, limit), state: document.readyState})"""
_DOM_COUNT = """limit => {
    const pending = [document]; let count = 0;
    while (pending.length) {
        const node = pending.pop();
        if (++count > limit) return false;
        for (let child = node.lastChild; child; child = child.previousSibling) pending.push(child);
        if (node.content) pending.push(node.content);
        if (node.shadowRoot) pending.push(node.shadowRoot);
    }
    return true;
}"""
_TEXT = """limit => {
    const walker = document.createTreeWalker(document.body || document.documentElement,
                                             NodeFilter.SHOW_TEXT);
    const rendered = new WeakMap();
    let out = '', node, truncated = false;
    while ((node = walker.nextNode())) {
        const parent = node.parentElement;
        if (!parent || parent.closest('script,style,noscript,template,form,input,textarea,select,[contenteditable]')) continue;
        if (!parent.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
        // A text node's parent can have a rectangle even while the native
        // parser hides that particular text (e.g. closed details children).
        // Native innerText answers this without rewriting source HTML.
        let visibleText = rendered.get(parent);
        if (visibleText === undefined) {
            visibleText = parent.innerText || '';
            rendered.set(parent, visibleText);
        }
        const text = node.textContent;
        if (!text.trim() || !visibleText.includes(text.trim())) continue;
        const remaining = limit - out.length;
        if (text.length > remaining) { out += text.slice(0, remaining); truncated = true; break; }
        out += text;
        if (out.length < limit) out += '\\n';
    }
    return {text: out.slice(0, limit), truncated};
}"""
_LINK_BATCH = """bounds => {
    const anchors = document.querySelectorAll('a[href]');
    const end = Math.min(anchors.length, bounds.cursor + bounds.size);
    const links = [];
    const visibleLabel = anchor => {
        const walker = document.createTreeWalker(anchor, NodeFilter.SHOW_TEXT);
        let label = '', node;
        while ((node = walker.nextNode()) && label.length < bounds.text) {
            const parent = node.parentElement;
            if (!parent || parent.closest('script,style,noscript,template,form,[contenteditable]') ||
                !parent.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
            const value = node.textContent;
            if (!value.trim() || !(parent.innerText || '').includes(value.trim())) continue;
            label += value.slice(0, bounds.text - label.length);
        }
        return label;
    };
    for (let index = bounds.cursor; index < end; index++) {
        const anchor = anchors[index];
        if (anchor.closest('form,[contenteditable]') ||
            !anchor.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) continue;
        const href = anchor.getAttribute('href');
        if (href.length > bounds.url) continue;
        links.push({url: href, text: visibleLabel(anchor)});
    }
    return {links, cursor: end, done: end === anchors.length};
}"""


def _check_render_source(config: BrowserConfig, source: str) -> None:
    try:
        oversized = (type(source) is not str or len(source) > config.render_source_limit
                     or len(source.encode("utf-8")) > config.render_source_limit)
    except UnicodeError:
        oversized = True
    if oversized:
        raise BrowserError(BrowserFailure.SIZE)


def _serve(pipe, config: BrowserConfig, *, _prepare=None):
    """Private worker entry; _prepare exists solely for offline fixture tests."""
    driver = browser = context = None
    failure = BrowserFailure.LAUNCH
    try:
        # Never propagate Playwright diagnostics into ordinary app logs.
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
        for key in ("DEBUG", "PWDEBUG", "SSLKEYLOGFILE", "NODE_OPTIONS",
                    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
                    "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            os.environ.pop(key, None)
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
        driver = sync_playwright().start()
        browser = driver.chromium.launch(headless=True, chromium_sandbox=True,
                                         timeout=config.operation_timeout_seconds * 1000)
        context = browser.new_context(java_script_enabled=False, service_workers="block",
                                      accept_downloads=False, offline=True)
        context.set_default_timeout(config.operation_timeout_seconds * 1000)
        context.set_default_navigation_timeout(config.operation_timeout_seconds * 1000)
        # Every intercepted HTTP request is denied, including redirects and
        # subresources. Do not continue/fetch after a DNS precheck.
        context.route("**/*", lambda route: route.abort("blockedbyclient"))
        context.route_web_socket("**/*", lambda socket: socket.close())
        page = context.new_page()
        page.on("dialog", lambda dialog: dialog.dismiss())
        page.on("popup", lambda popup: popup.close())
        crashed = [False]
        page.on("crash", lambda _: crashed.__setitem__(0, True))
        browser.on("disconnected", lambda _: crashed.__setitem__(0, True))
        if _prepare is not None:
            _prepare(page)
        pipe.send((True, None))
        failure = BrowserFailure.PROVIDER
        while True:
            operation, argument = pipe.recv()
            if operation == "close":
                break
            if crashed[0] or not browser.is_connected():
                raise BrowserError(BrowserFailure.CRASH)
            if page.is_closed():
                raise BrowserError(BrowserFailure.CLOSED)
            try:
                if operation == "open_url":
                    # Even direct host adapter calls cannot enable navigation.
                    raise BrowserError(BrowserFailure.NETWORK_BOUNDARY)
                if operation == "render_document":
                    _check_render_source(config, argument)
                    # Each source gets a fresh non-persistent context. The
                    # route is installed before page creation and can fulfill
                    # only the first exact main-frame synthetic navigation.
                    context.close()
                    context = browser.new_context(java_script_enabled=False,
                                                  service_workers="block",
                                                  accept_downloads=False, offline=True)
                    context.set_default_timeout(config.operation_timeout_seconds * 1000)
                    context.set_default_navigation_timeout(config.operation_timeout_seconds * 1000)
                    served = [False]

                    def route_document(route):
                        request = route.request
                        try:
                            eligible = (not served[0] and request.url == _DOCUMENT_URL
                                        and request.method == "GET"
                                        and request.is_navigation_request()
                                        and request.frame == page.main_frame)
                        except Exception:
                            eligible = False
                        if eligible:
                            served[0] = True
                            route.fulfill(status=200, body=argument, headers={
                                "Content-Type": "text/html; charset=utf-8",
                                "Content-Security-Policy": _CSP,
                                "Cache-Control": "no-store",
                                "X-Content-Type-Options": "nosniff",
                            })
                        else:
                            route.abort("blockedbyclient")

                    context.route("**/*", route_document)
                    context.route_web_socket("**/*", lambda socket: socket.close())
                    page = context.new_page()
                    page.on("dialog", lambda dialog: dialog.dismiss())
                    page.on("popup", lambda popup: popup.close())
                    page.on("crash", lambda _: crashed.__setitem__(0, True))
                    page.goto(_DOCUMENT_URL, wait_until="load",
                              timeout=config.operation_timeout_seconds * 1000)
                    if not served[0] or page.url != _DOCUMENT_URL:
                        raise BrowserError(BrowserFailure.NETWORK_BOUNDARY)
                    if not page.evaluate(_DOM_COUNT, config.max_dom_nodes):
                        raise BrowserError(BrowserFailure.SIZE)
                    pipe.send((True, None))
                    continue
                if operation == "current_page":
                    data = page.evaluate(_METADATA, config.max_title_chars)
                elif operation == "extract_text":
                    data = page.evaluate(_TEXT, config.max_text_chars)
                elif operation == "link_batch":
                    cursor, size = argument
                    if (type(cursor) is not int or type(size) is not int or cursor < 0
                            or cursor > config.max_dom_nodes or not 1 <= size <= 64):
                        raise BrowserError(BrowserFailure.PROVIDER)
                    data = page.evaluate(_LINK_BATCH, {"cursor": cursor, "size": size,
                                                       "url": config.max_url_chars,
                                                       "text": config.max_link_text_chars})
                else:
                    raise BrowserError(BrowserFailure.PROVIDER)
                pipe.send((True, data))
            except PlaywrightTimeout:
                raise BrowserError(BrowserFailure.TIMEOUT) from None
    except Exception as error:
        category = error.failure if isinstance(error, BrowserError) else failure
        try:
            pipe.send((False, category.value))
        except (EOFError, BrokenPipeError, OSError):
            pass
    finally:
        # Parent independently verifies observed process exits. Cleanup
        # exceptions are not treated as proof of successful termination.
        for resource in (context, browser, driver):
            if resource is not None:
                try:
                    resource.stop() if resource is driver else resource.close()
                except Exception:
                    pass
        pipe.close()


class PlaywrightBackend:
    network_isolated = False

    def __init__(self, config: BrowserConfig, *, _worker_target=_serve):
        config.validate()
        self._config = config
        self._worker_target = _worker_target
        self._owner = (os.getpid(), threading.get_ident())
        self._worker = self._pipe = self._root = None
        self._observed = {}
        self._closed = False
        self._cleanup_failed = False
        self._sender_thread = None
        self._reader_thread = None

    def _require_owner(self):
        if self._owner != (os.getpid(), threading.get_ident()):
            raise BrowserError(BrowserFailure.OWNERSHIP)

    def _remember_tree(self):
        import psutil
        if self._root is None:
            return
        try:
            processes = [self._root, *self._root.children(recursive=True)]
            for process in processes:
                self._observed[(process.pid, process.create_time())] = process
        except psutil.NoSuchProcess:
            pass
        except Exception:
            self._cleanup_failed = True
            raise BrowserError(BrowserFailure.CLEANUP) from None

    def start(self):
        self._require_owner()
        if self._closed:
            raise BrowserError(BrowserFailure.CLOSED)
        if self._worker is not None:
            return
        try:
            import psutil
            spawn = multiprocessing.get_context("spawn")
            self._pipe, child = spawn.Pipe()
            self._worker = spawn.Process(target=self._worker_target, args=(child, self._config), daemon=True)
            self._worker.start()
            child.close()
            self._root = psutil.Process(self._worker.pid)
            self._remember_tree()
            self._receive()
        except Exception as error:
            failure = error.failure if isinstance(error, BrowserError) else BrowserFailure.LAUNCH
            self.close()
            raise BrowserError(failure) from None
        except BaseException:
            self.close()
            raise

    def _receive(self):
        deadline = time.monotonic() + self._config.operation_timeout_seconds
        messages = queue.Queue()
        pipe = self._pipe
        def receive():
            try:
                messages.put(pipe.recv())
            except (EOFError, OSError):
                messages.put((False, BrowserFailure.CRASH.value))
        self._reader_thread = threading.Thread(target=receive, daemon=True)
        self._reader_thread.start()
        try:
            while time.monotonic() < deadline:
                self._remember_tree()
                try:
                    ok, data = messages.get(timeout=min(.05, max(0, deadline - time.monotonic())))
                except queue.Empty:
                    pass
                else:
                    if not ok:
                        raise BrowserError(BrowserFailure(data))
                    return data
                if not self._worker.is_alive():
                    raise BrowserError(BrowserFailure.CRASH)
            raise BrowserError(BrowserFailure.TIMEOUT)
        except (EOFError, BrokenPipeError, OSError):
            raise BrowserError(BrowserFailure.CRASH) from None

    def _request(self, operation, argument=None):
        self._require_owner()
        if self._closed or self._worker is None:
            raise BrowserError(BrowserFailure.CLOSED)
        try:
            # A bounded document can exceed pipe capacity. A stuck renderer
            # must not strand the owning thread inside send() without a deadline.
            pipe = self._pipe
            def send():
                try:
                    pipe.send((operation, argument))
                except (EOFError, BrokenPipeError, OSError):
                    pass
            sender = threading.Thread(target=send, daemon=True)
            self._sender_thread = sender
            sender.start()
            return self._receive()
        except Exception as error:
            failure = error.failure if isinstance(error, BrowserError) else BrowserFailure.PROVIDER
            self.close()
            raise BrowserError(failure) from None
        except BaseException:
            self.close()
            raise

    def open_url(self, url):
        self._require_owner()
        raise BrowserError(BrowserFailure.NETWORK_BOUNDARY)

    def render_document(self, source):
        self._require_owner()
        _check_render_source(self._config, source)
        return self._request("render_document", source)

    def current_page(self):
        data = self._request("current_page")
        state = "blank" if data["url"] == "about:blank" else "ready" if data["state"] == "complete" else "loading"
        return PageMetadata(data["url"], data["title"], state)

    def extract_text(self):
        data = self._request("extract_text")
        return PageText(data["text"], data["truncated"])

    def list_links(self):
        # Direct adapter use has no trusted fetched URL for canonicalization;
        # BrokeredBrowserBackend uses link_batch and filters before its quota.
        links = []
        seen = set()
        cursor = 0
        done = False
        while not done and len(links) <= self._config.max_links:
            batch, cursor, done = self.link_batch(cursor, min(64, self._config.max_dom_nodes))
            for link in batch:
                if link.url not in seen:
                    seen.add(link.url)
                    links.append(link)
                    if len(links) > self._config.max_links:
                        break
        return PageLinks(tuple(links[:self._config.max_links]),
                         len(links) > self._config.max_links)

    def link_batch(self, cursor: int, size: int):
        data = self._request("link_batch", (cursor, size))
        return (tuple(PageLink(item["url"], item["text"]) for item in data["links"]),
                data["cursor"], data["done"])

    def close(self):
        self._require_owner()
        if self._closed:
            if self._cleanup_failed:
                raise BrowserError(BrowserFailure.CLEANUP)
            return
        self._closed = True
        try:
            if self._worker is not None and self._worker.pid is not None:
                import psutil
                try:
                    self._remember_tree()
                except BrowserError:
                    # Still terminate the known owned root/tree when process
                    # enumeration fails; retain categorical cleanup failure.
                    pass
                if self._sender_thread is not None and self._sender_thread.is_alive():
                    # Never race two writers or block cleanup behind a large
                    # stalled document write. Terminate the owned worker first.
                    self._worker.terminate()
                else:
                    try:
                        self._pipe.send(("close", None))
                    except (BrokenPipeError, EOFError, OSError):
                        pass
                self._worker.join(timeout=2)
                processes = list(self._observed.values())
                # psutil Process objects fence PID reuse with creation time.
                for process in reversed(processes):
                    try:
                        if process.is_running():
                            process.kill()
                    except psutil.NoSuchProcess:
                        pass
                # multiprocessing owns waitpid for the worker. psutil must
                # not reap that direct child on POSIX before Process.join().
                descendants = [p for p in processes if p.pid != self._worker.pid]
                _, alive = psutil.wait_procs(descendants, timeout=2)
                if self._worker.is_alive():
                    self._worker.kill()
                self._worker.join(timeout=2)
                if alive or self._worker.is_alive():
                    self._cleanup_failed = True
                else:
                    self._worker.close()
        except Exception:
            self._cleanup_failed = True
        finally:
            if self._pipe is not None:
                try:
                    self._pipe.close()
                except Exception:
                    self._cleanup_failed = True
            for thread in (self._sender_thread, self._reader_thread):
                if thread is not None:
                    thread.join(timeout=.2)
                    if thread.is_alive():
                        self._cleanup_failed = True
        if self._cleanup_failed:
            raise BrowserError(BrowserFailure.CLEANUP) from None
