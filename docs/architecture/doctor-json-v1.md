# Doctor JSON diagnostics v1

`agentcore doctor --json` emits one JSON object on stdout. The existing
`agentcore doctor` output and exit behavior remain unchanged: that command
already emits an unversioned legacy JSON report. The explicit option supplies
a bounded diagnostic schema rather than exporting the legacy configuration
and provider descriptions.

## Schema version 1

The top-level keys, in order, are `schema_version` (integer, exactly `1`),
`ok` (boolean), and `checks` (array of exactly five objects). No other fields
are emitted. Checks appear in this fixed order:

1. `configuration`: existing configuration loading and validation.
2. `runtime`: existing Agent construction and shutdown.
3. `computer_control`: existing integration connection and health checks.
4. `model`: existing provider description; no generation or connectivity probe.
5. `providers`: existing available-provider catalogue inspection.

Each check has exactly `name`, `outcome`, and `message`, in that order.
`name` is one of the five identifiers above. Outcomes and messages are fixed:

| Outcome | Message |
|---|---|
| `ok` | `Check passed.` |
| `not_configured` | `No model is configured.` |
| `unavailable` | `Component is unavailable.` |
| `error` | `Check failed; inspect configuration locally.` |
| `not_checked` | `Check was not run.` |

Messages are at most 64 characters; the entire stdout document is below 2 KiB.
No timestamps, run IDs, private configuration, names supplied by integrations,
model/endpoint names, credential values or variable names, filesystem paths,
raw integration details, exceptions or tracebacks are exported. Fixed messages
provide redaction by construction rather than attempting to scrub free text.
Malformed results and exceptions become categorical errors. A failing check
does not stop the other checks once runtime construction succeeds.

## Exit status and scope

- `0`: all checks are `ok` or `not_configured`; `ok` is true.
- `1`: a component is unavailable, a check fails, or runtime construction or
  shutdown fails; `ok` is false.
- `2`: configuration cannot be loaded/validated; later checks are `not_checked`.

The computer-control adapter is currently an unavailable stub, so a default
`doctor --json` invocation returns `1`. Legacy `doctor` still returns `0` for
that report. A model explicitly configured but unavailable is a diagnostic
failure; an intentionally unconfigured model is not.

The command reuses the legacy Agent construction, adapter checks, model
description and provider catalogue. It does not run objectives, tools or model
generation, and adds no network probes, retries, permission changes or durable
state actions. Existing configured runtime initialization, including memory
initialization, retains the legacy doctor's behavior. Logging is disabled for
the full diagnostic lifecycle, including critical/custom-level records, and the
previous disable threshold is restored afterward, even on failure. Diagnostic
mode also replaces logging configuration so a configured log file is not opened.
