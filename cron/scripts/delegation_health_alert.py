#!/usr/bin/env python3
"""Emit delegation-health alerts for an explicitly configured no-agent cron job.

Uses the active profile's canonical receipt summarizer. This file is a script
payload, not a scheduler or delivery mechanism: it must be installed through
the operator's approved runtime and job setup before cron can invoke it.
"""
import sys


def main() -> int:
    try:
        from hermes_constants import get_hermes_home
        from hermes_cli.delegation_health import format_alert, read_summary

        summary = read_summary(get_hermes_home() / 'state.db', hours=24)
        alert = format_alert(summary)
    except Exception:
        # Do not expose arbitrary database/receipt content in a notification.
        # The no-agent runner turns nonzero script exits into a failure alert.
        print('Delegation health unavailable; inspect the profile database and runtime.', file=sys.stderr)
        return 1
    if alert is not None:
        print(alert)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
