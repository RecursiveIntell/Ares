"""
Hermes CLI - Unified command-line interface for Hermes Agent.

Provides subcommands for:
- hermes chat          - Interactive chat (same as ./hermes)
- hermes gateway       - Run gateway in foreground
- hermes gateway start - Start gateway service
- hermes gateway stop  - Stop gateway service
- hermes setup         - Interactive setup wizard
- hermes status        - Show status of all components
- hermes cron          - Manage cron jobs
"""

import os
import sys

__version__ = "0.20.6"
__release_date__ = "2026.8.27"


def _dispatch_inert_specialist_command() -> None:
    """Route the explicit specialist command before ordinary CLI startup.

    Importing ``hermes_cli.main`` performs recovery, profile and environment
    setup. Keep this small pre-import scanner here and derive option arity
    from the canonical top-level parser metadata.
    """
    if not _is_hermes_cli_entrypoint():
        return
    argv = sys.argv[1:]
    # Preserve the established lightweight package import for every ordinary
    # invocation. The exact token can only be a command or an option value;
    # canonical metadata is needed only to distinguish those cases.
    if "specialists" not in argv:
        return
    from hermes_cli._parser import top_level_option_metadata

    metadata = top_level_option_metadata()
    prefix_options: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            if index + 1 < len(argv) and argv[index + 1] == "specialists":
                if prefix_options:
                    _refuse_specialist_global_options(prefix_options)
                from hermes_cli.specialists import run_standalone

                raise SystemExit(run_standalone(argv[index + 2 :]))
            return
        if not token.startswith("-"):
            if token != "specialists":
                return
            if prefix_options:
                _refuse_specialist_global_options(prefix_options)
            from hermes_cli.specialists import run_standalone

            raise SystemExit(run_standalone(argv[index + 1 :]))

        if "=" in token:
            name = token.split("=", 1)[0]
            if name.startswith("--") and name not in metadata:
                matches = [option for option in metadata if option.startswith(name)]
                if len(matches) == 1:
                    name = matches[0]
            prefix_options.append(name)
            index += 1
            continue

        option_name = token
        kind = metadata.get(option_name)
        if kind is None and token.startswith("--"):
            matches = [
                option
                for option in metadata
                if option.startswith("--") and option.startswith(token)
            ]
            if len(matches) == 1:
                option_name = matches[0]
                kind = metadata[option_name]
        if kind is not None:
            prefix_options.append(option_name)
            if kind == "value" and index + 1 < len(argv):
                index += 2
                continue
            if (
                kind == "optional"
                and index + 1 < len(argv)
                and not argv[index + 1].startswith("-")
            ):
                index += 2
                continue
            index += 1
            continue

        # Accept attached short-option values as part of the option token,
        # while still rejecting that global option if a specialist command
        # follows it.
        attached = next(
            (
                option
                for option in metadata
                if option.startswith("-")
                and not option.startswith("--")
                and len(option) == 2
                and token.startswith(option)
                and len(token) > 2
            ),
            None,
        )
        prefix_options.append(attached or token)
        index += 1


def _is_hermes_cli_entrypoint() -> bool:
    """Avoid claiming argv from programs that merely import this package."""
    original = getattr(sys, "orig_argv", ())
    index = 1
    value_options = {"-W", "-X", "--check-hash-based-pycs"}
    while index < len(original):
        token = original[index]
        if token == "-m":
            return (
                index + 1 < len(original) and original[index + 1] == "hermes_cli.main"
            )
        if token in {"-c", "--"}:
            return False
        if token in value_options:
            index += 2
            continue
        if token.startswith(("-W", "-X")) and len(token) > 2:
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        break
    executable = original[index] if index < len(original) else sys.argv[0]
    try:
        return os.path.basename(os.fsdecode(executable)).lower() in {
            "hermes",
            "hermes.exe",
        }
    except (TypeError, ValueError):
        return False


def _refuse_specialist_global_options(options: list[str]) -> None:
    # os.write avoids triggering the normal UTF-8 stream repair just to emit
    # this fixed ASCII refusal.
    names = ", ".join(dict.fromkeys(options))
    os.write(
        2,
        f"hermes specialists: unsupported global option(s): {names}\n".encode(
            "ascii", "replace"
        ),
    )
    raise SystemExit(2)


_dispatch_inert_specialist_command()


def _ensure_utf8():
    """Force UTF-8 stdout/stderr to prevent UnicodeEncodeError crashes.

    Several environments select a legacy, non-UTF-8 encoding for the standard
    streams:

    - Windows services and terminals default to cp1252.
    - Linux hosts with a latin-1 / C / POSIX locale (common on minimal Debian
      installs and Raspberry Pi) select latin-1 or ASCII.

    The CLI prints box-drawing characters (┌│├└─) and the ⚕ glyph in the setup
    wizard, doctor, and status banners. Encoding those under a non-UTF-8 codec
    raises an unhandled UnicodeEncodeError that crashes the command before it
    can even start — e.g. `hermes setup` on a fresh Pi.

    This runs at import time so it protects every CLI subcommand, on any
    platform. It re-wraps stdout/stderr as UTF-8 when their encoding is not
    already UTF-8, preferring TextIOWrapper.reconfigure() so the existing
    stream object is fixed in place (cached `sys.stdout` references keep
    working) and falling back to reopening the file descriptor with
    closefd=False (the CPython-recommended safe variant).

    No-op when the streams are already UTF-8: a healthy UTF-8 system sees no
    stream change and no environment mutation.

    Note: this is intentionally the earliest, platform-agnostic guard.
    hermes_cli/stdio.py::configure_windows_stdio() runs later from the entry
    points and layers on the Windows-only extras (console code-page flip,
    EDITOR default, PATH augmentation); its stream reconfiguration is a
    harmless idempotent no-op once we have already repaired the streams here.
    """
    repaired = False

    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        try:
            encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
            if encoding == "utf8":
                continue

            # Preferred: reconfigure the existing TextIOWrapper in place. This
            # preserves object identity so any code already holding a reference
            # to the old sys.stdout benefits from the repair too.
            reconfigure = getattr(stream, "reconfigure", None)
            if callable(reconfigure):
                reconfigure(encoding="utf-8", errors="replace")
                repaired = True
                continue

            # Fallback: reopen the underlying file descriptor as UTF-8. Used
            # for streams that don't expose reconfigure() (e.g. some wrapped
            # or replaced streams). closefd=False keeps the original fd open.
            new_stream = open(
                stream.fileno(), "w", encoding="utf-8",
                errors="replace", buffering=1, closefd=False,
            )
            setattr(sys, stream_name, new_stream)
            repaired = True
        except (AttributeError, OSError, ValueError):
            pass

    # Only nudge child processes toward UTF-8 when we actually detected a
    # non-UTF-8 locale. On a healthy UTF-8 host children inherit UTF-8 from the
    # locale already, so leave the environment untouched (minimal footprint).
    if repaired:
        os.environ.setdefault("PYTHONUTF8", "1")
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")


_ensure_utf8()
