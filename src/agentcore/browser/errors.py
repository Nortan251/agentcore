"""Categorical browser failures. Never retain provider exceptions or URLs."""

from enum import Enum


class BrowserFailure(str, Enum):
    DISABLED = "browser_disabled"
    NETWORK_BOUNDARY = "browser_network_boundary_unavailable"
    DESTINATION = "browser_destination_denied"
    NAVIGATION_NOT_AUTHORIZED = "browser_navigation_not_authorized"
    DNS = "browser_dns_failed"
    LAUNCH = "browser_launch_failed"
    TIMEOUT = "browser_timeout"
    CLOSED = "browser_closed"
    CRASH = "browser_crashed"
    PROVIDER = "browser_provider_failed"
    CLEANUP = "browser_cleanup_unverified"
    OWNERSHIP = "browser_ownership_mismatch"
    CONTRACT = "browser_composition_invalid"
    FETCH = "browser_fetch_failed"
    RESPONSE = "browser_response_rejected"
    SIZE = "browser_response_too_large"
    REDIRECT = "browser_redirect_rejected"


class BrowserError(Exception):
    def __init__(self, failure: BrowserFailure):
        self.failure = failure
        super().__init__(failure.value)
