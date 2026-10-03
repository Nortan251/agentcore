import pytest

from agentcore.browser.errors import BrowserError, BrowserFailure
from agentcore.browser.network import BrowserNetworkPolicy, canonical_url, display_url, public_address


@pytest.mark.parametrize("url,expected", [
    ("https://Example.COM", "https://example.com/"),
    ("http://example.com:80/a?x=1#fragment", "http://example.com/a?x=1"),
    ("https://example.com:443/a", "https://example.com/a"),
    ("https://8.8.8.8/", "https://8.8.8.8/"),
    ("https://[2001:4860:4860::8888]/", "https://[2001:4860:4860::8888]/"),
])
def test_canonical_public(url, expected):
    assert canonical_url(url) == expected


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "file:///C:/Windows/system.ini", "ftp://example.com/a",
    "javascript:alert(1)", "data:text/html,hi", "about:blank", "ws://example.com/",
    "https://localhost/", "http://a.localhost/", "http://localhost.localdomain/",
    "https://machine.local/", "http://intranet/", "http://machine.internal/",
    "http://127.0.0.1/", "http://127.255.255.255/", "http://[::1]/",
    "http://10.0.0.1/", "http://172.16.0.1/", "http://172.31.255.255/",
    "http://192.168.1.1/", "http://169.254.169.254/", "http://100.64.0.1/",
    "http://0.0.0.0/", "http://192.0.2.1/", "http://198.51.100.1/",
    "http://203.0.113.1/", "http://224.0.0.1/", "http://255.255.255.255/",
    "http://[fc00::1]/", "http://[fd12::1]/", "http://[fe80::1]/",
    "http://[::]/", "http://[::ffff:127.0.0.1]/", "http://[::ffff:8.8.8.8]/",
    "http://[64:ff9b::808:808]/", "http://[2002:808:808::1]/",
    "http://[2001:db8::1]/", "http://[ff02::1]/", "http://[fe80::1%25eth0]/",
    "http://user:password@example.com/", "http://user@example.com/",
    "http://example.com@127.0.0.1/", "http://example.com\\@127.0.0.1/",
    "http://2130706433/", "http://0177.0.0.1/", "http://127.1/",
    "http://0x7f000001/", "http://0x7f.0.0.1/", "http://127.000.000.001/",
    "http://%31%32%37.0.0.1/", "http://example.com./", "http://example..com/",
    "http://-example.com/", "http://example.com:/", "http://example.com:8080/",
    "http://example.com:abc/", "http://[::1", "http:///example.com/",
    " http://example.com/", "http://example.com/\n", "http://éxample.com/",
    "http://example.com/\x00", "http://example.com/\x7f",
])
def test_forbidden_and_ambiguous_urls(url):
    with pytest.raises(BrowserError) as error:
        canonical_url(url)
    assert error.value.failure is BrowserFailure.DESTINATION
    assert url not in str(error.value)


@pytest.mark.parametrize("addresses", [[], ["127.0.0.1"], ["10.1.1.1"], ["::1"],
                                         ["fe80::1"], ["8.8.8.8", "192.168.0.1"], ["bad"]])
def test_dns_forbidden_and_mixed(addresses):
    policy = BrowserNetworkPolicy(lambda host, port: addresses)
    with pytest.raises(BrowserError):
        policy.screen_request("https://example.com/")


def test_dns_failure_is_categorical():
    def resolver(*_):
        raise RuntimeError("credential query=SECRET")
    with pytest.raises(BrowserError) as error:
        BrowserNetworkPolicy(resolver).screen_request("https://example.com/")
    assert str(error.value) == BrowserFailure.DNS.value


def test_redirect_and_subresources_are_independent_requests():
    calls = []
    def resolve(host, port):
        calls.append(host)
        return ["8.8.8.8"] if host == "example.com" else ["127.0.0.1"]
    policy = BrowserNetworkPolicy(resolve)
    assert policy.screen_request("https://example.com/") == "https://example.com/"
    for redirect_or_resource in ("https://attacker.com/redirect", "https://attacker.com/image.png"):
        with pytest.raises(BrowserError):
            policy.screen_request(redirect_or_resource)
    assert calls == ["example.com", "attacker.com", "attacker.com"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "CONNECT", "OPTIONS", "get"])
def test_write_methods_denied_before_dns(method):
    def resolver(*_):
        pytest.fail("write method must not resolve DNS")
    with pytest.raises(BrowserError):
        BrowserNetworkPolicy(resolver).screen_request("https://example.com/", method)


def test_dns_precheck_does_not_prove_next_connection():
    addresses = iter((["8.8.8.8"], ["127.0.0.1"]))
    policy = BrowserNetworkPolicy(lambda *_: next(addresses))
    assert policy.screen_request("https://example.com/")
    with pytest.raises(BrowserError):
        policy.screen_request("https://example.com/")
    # A browser could do its own second lookup. Backend must remain disabled.
    from agentcore.browser.playwright_backend import PlaywrightBackend
    assert PlaywrightBackend.network_isolated is False


def test_display_url_omits_secrets():
    assert display_url("https://example.com/secret?token=SECRET#secret") == "https://example.com/"
    assert display_url("http://user:password@example.com/") == "[redacted]"


def test_address_global_is_not_enough_for_multicast():
    assert not public_address("224.0.0.1")
