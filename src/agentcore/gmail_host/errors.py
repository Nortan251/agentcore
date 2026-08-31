"""Closed operator failures; never format private input or exception details."""

from enum import Enum


class HostFailure(str, Enum):
    CONFIGURATION = "host_configuration_unavailable"
    STORAGE = "host_storage_unavailable"
    PRIVATE_TERMINAL = "private_interactive_terminal_required"
    INVALID_INPUT = "smoke_test_input_rejected"
    DISABLED = "live_send_disabled"
    CONNECTION = "current_gmail_send_connection_required"
    EXISTING = "one_preparation_already_exists"
    MISSING = "exact_preparation_required"
    ACTION = "action_inspection_required"
    UNCERTAIN = "reconcile_required_do_not_send_again"
    CONFIRMATION = "fresh_human_confirmation_required"
    OWNERSHIP = "host_ownership_mismatch"
    UNAVAILABLE = "host_operation_failed"


class GmailHostError(Exception):
    def __init__(self, reason: HostFailure) -> None:
        self.reason = reason
        super().__init__(reason.value)
