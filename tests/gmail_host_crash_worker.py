"""Crash worker: synthetic operator/OAuth and guarded memory sockets only."""

import os
import sys
from pathlib import Path

from agentcore.action_journal import ActionState
from agentcore.gmail_host.composition import GmailHostMode
from gmail_host_fakes import BODY, SUBJECT, FakeOperator, HostHarness
from gmail_mutation_http_fakes import HttpBoundary, PROFILE_PATH


def main():
    directory, action_id, cut = sys.argv[1:]
    boundary = HttpBoundary()
    boundary.install()  # denies DNS/socket egress before opening any services
    if cut in {"preparation", "preparation_temp"}:
        with HostHarness(directory).host(GmailHostMode.PREPARE, operator=FakeOperator(SUBJECT, BODY)) as host:
            # Kill the real process on either side of payload publication,
            # before host correlation can be committed.
            if cut == "preparation":
                host.services.storage.save_once = lambda slot: os._exit(71)
            else:
                replace = os.replace

                def abandon_payload_file(source, destination):
                    if Path(destination).parent == host.services.storage.directory / "payloads":
                        os._exit(71)  # fsynced temporary payload, before replace
                    return replace(source, destination)

                os.replace = abandon_payload_file
            host.prepare_smoke_test()
        raise AssertionError("Synthetic preparation crash point was not reached")
    with HostHarness(directory).host(GmailHostMode.LIVE_SEND, operator=FakeOperator("CHALLENGE")) as host:
        profiles = 0

        def at_response(request):
            nonlocal profiles
            if request.path == PROFILE_PATH:
                profiles += 1
                if cut == "preflight" and profiles == 2:
                    assert host.services.actions.load(action_id).state is ActionState.IN_FLIGHT
                    os._exit(71)
            if cut == "post" and request.method == "POST":
                assert host.services.actions.load(action_id).state is ActionState.IN_FLIGHT
                os._exit(71)

        boundary.on_response = at_response
        create = host.services.actions.create

        def create_authorized(record):
            created = create(record)
            if cut == "authorized":
                assert created.state is ActionState.AUTHORIZED and not created.attempts
                os._exit(71)
            return created

        host.services.actions.create = create_authorized
        compare_and_swap = host.services.actions.compare_and_swap

        def publish(action_id, version, candidate):
            if cut == "publication" and candidate.state is ActionState.SUCCEEDED:
                os._exit(71)
            return compare_and_swap(action_id, version, candidate)

        host.services.actions.compare_and_swap = publish
        host.send_smoke_test(action_id)
    raise AssertionError("Synthetic crash point was not reached")


if __name__ == "__main__":
    main()
