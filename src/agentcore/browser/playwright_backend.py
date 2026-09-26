"""Owned Playwright worker with hard parent deadlines.

The default backend deliberately DOES NOT advertise network isolation.
Request abortion/offline mode are defense in depth, not an SSRF guarantee.
Only trusted host integration tests start this backend directly.
"""

import multiprocessing
import queue
import os
import threading
import time

from .backend import PageLink, PageLinks, PageMetadata, PageText
from .config import BrowserConfig
from .errors import BrowserError, BrowserFailure

# Fixed original adapter scripts, never model supplied. Bound the result in
# Chromium before IPC. Main document only; no iframe or arbitrary DOM dump.
_METADATA = """limit => ({url: location.href.slice(0, 2049),
    title: document.title.slice(0, limit), state: document.readyState})"""
_TEXT = """bounds => {
    const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
    let out = '', scanned = 0, node, truncated = false;
    while ((node = walker.nextNode())) {
        if (++scanned > bounds.nodes) { truncated = true; break; }
        const p = node.parentElement;
        if (!p || p.closest('script,style,noscript,template,form,input,textarea,select,[contenteditable]')) continue;
        if (!p.getClientRects().length) continue;
        let visible = true;
        for (let e = p; e; e = e.parentElement) {
            const s = getComputedStyle(e);
            if (s.display === 'none' || s.visibility !== 'visible' || s.opacity === '0') { visible = false; break; }
        }
        if (!visible) continue;
        const remaining = bounds.chars - out.length;
        const text = node.textContent;
        if (text.length > remaining) { out += text.slice(0, remaining); truncated = true; break; }
        out += text;
        if (out.length < bounds.chars) out += '\\n';
    }
    return {text: out.slice(0, bounds.chars), truncated};
}"""
_LINKS = """bounds => {
    const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_ELEMENT);
    const links = [], seen = new Set(); let scanned = 0, node, truncated = false;
    while ((node = walker.nextNode())) {
        if (++scanned > bounds.nodes) { truncated = true; break; }
        if (node.tagName !== 'A' || !(node.hasAttribute('data-agentcore-href') || node.hasAttribute('href')) || node.closest('form,[contenteditable]')) continue;
        if (!node.getClientRects().length) continue;
        let visible = true;
        for (let e = node; e; e = e.parentElement) {
            const s = getComputedStyle(e);
            if (s.display === 'none' || s.visibility !== 'visible' || s.opacity === '0') { visible = false; break; }
        }
        if (!visible) continue;
        const href = node.getAttribute('data-agentcore-href') ?? node.getAttribute('href');
        if (href.length > bounds.url || seen.has(href)) continue;
        seen.add(href);
        links.push({url: href, text: node.innerText.slice(0, bounds.text)});
        if (links.length > bounds.links) { truncated = true; break; }
    }
    return {links, truncated};
}"""


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
                    if type(argument) is not str or len(argument) > config.max_body_bytes * 6 + 1024:
                        raise BrowserError(BrowserFailure.SIZE)
                    page.set_content(argument, wait_until="load",
                                     timeout=config.operation_timeout_seconds * 1000)
                    pipe.send((True, None))
                    continue
                if operation == "current_page":
                    data = page.evaluate(_METADATA, config.max_title_chars)
                elif operation == "extract_text":
                    data = page.evaluate(_TEXT, {"chars": config.max_text_chars, "nodes": config.max_dom_nodes})
                elif operation == "list_links":
                    data = page.evaluate(_LINKS, {"links": config.max_links, "url": config.max_url_chars,
                                                "text": config.max_link_text_chars, "nodes": config.max_dom_nodes})
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
        return self._request("render_document", source)

    def current_page(self):
        data = self._request("current_page")
        state = "blank" if data["url"] == "about:blank" else "ready" if data["state"] == "complete" else "loading"
        return PageMetadata(data["url"], data["title"], state)

    def extract_text(self):
        data = self._request("extract_text")
        return PageText(data["text"], data["truncated"])

    def list_links(self):
        data = self._request("list_links")
        return PageLinks(tuple(PageLink(item["url"], item["text"]) for item in data["links"]), data["truncated"])

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
