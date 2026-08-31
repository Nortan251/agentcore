"""The operator CLI is opt-in; importing AgentCore never executes it."""

from agentcore.gmail_host.cli import main


if __name__ == "__main__":
    exit_code = 2
    try:
        exit_code = main()
    except BaseException:
        # A broken output channel or teardown failure must not turn a safe
        # categorical error into a traceback containing chained private data.
        pass
    raise SystemExit(exit_code)
