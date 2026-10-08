#!/usr/bin/env python3
"""Offline stand-in for the GitHub CLI, driven by a JSON fixture.

Response fixtures: {"body": ...} or {"pages": [...]} for a JSON response; {"raw": "..."} for a body
that is not JSON; {"status": 4xx, "message": "..."} for a JSON error, with optional "hints" (extra
"gh: ..." lines) and "pages" fetched before the failure; {"status": 4xx, "raw": "..."} for a non-JSON
error; {"exit": 1, "stderr": "..."} for a failure without an HTTP response; {"hang": true} for a request that
never returns (it touches FAKE_HANG_MARKER for the fake timeout); {"signal_parent": N} to
signal the calling script and linger. A list of fixtures is served in order, repeating the last one.
The --jq emulation uses the system jq, which prints "null" for null, pretty JSON for objects, and exact
big integers, where gh prints an empty line, compact JSON, and float64-rounded numbers.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REASONS = {403: "Forbidden", 404: "Not Found", 429: "Too Many Requests", 502: "Bad Gateway"}
COLOUR_START = "\x1b[1;39m"
COLOUR_END = "\x1b[0m"


def truthy(name: str) -> bool:
    return os.environ.get(name, "") not in ("", "0")


def unexpected(argv: list[str]) -> int:
    sys.stderr.write(f"unexpected gh call: {' '.join(argv)}\n")
    return 64


def parse_options(args: list[str]) -> tuple[dict, list[str]]:
    options: dict = {"method": "GET", "jq": None, "paginate": False, "fields": []}
    positional: list[str] = []
    remaining = iter(args)
    for arg in remaining:
        if arg == "--method":
            options["method"] = next(remaining)
        elif arg in ("--jq", "--json"):
            options[arg[2:]] = next(remaining)
        elif arg in ("-f", "-F", "--field", "--raw-field"):
            options["fields"].append(next(remaining))
        elif arg == "--paginate":
            options["paginate"] = True
        else:
            positional.append(arg)
    return options, positional


def next_response(key: str, responses: dict | list) -> dict:
    if isinstance(responses, dict):
        return responses
    state_path = os.environ["FAKE_GH_STATE"]
    state = {}
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as handle:
            state = json.load(handle)
    index = state.get(key, 0)
    state[key] = index + 1
    with open(state_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle)
    return responses[min(index, len(responses) - 1)]


def write_stdout(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


def emit(body: object, jq_filter: str | None) -> int:
    text = json.dumps(body)
    if truthy("GH_DEBUG") or truthy("DEBUG"):
        # Like gh's HTTP diagnostics, expose the response body on stderr.
        sys.stderr.write(text + "\n")
    if jq_filter is None:
        if body is not None:
            if truthy("CLICOLOR_FORCE") or os.environ.get("GH_FORCE_TTY"):
                # Like gh, colourise and re-indent JSON when colour or a terminal is forced.
                write_stdout(COLOUR_START + json.dumps(body, indent=2) + COLOUR_END + "\n")
            else:
                write_stdout(text + "\n")
        return 0
    import subprocess

    result = subprocess.run(["jq", "-r", jq_filter], input=text, capture_output=True, text=True, check=False)
    write_stdout(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


def respond(response: dict, options: dict) -> int:
    if response.get("hang"):
        # A request that never returns: declare the hang to the fake timeout, declare TERM when it arrives
        # and ignore it, so the fake timeout can apply its kill grace deterministically.
        import signal as _signal
        import time as _time

        marker = Path(os.environ["FAKE_HANG_MARKER"])
        _signal.signal(_signal.SIGTERM, lambda *_: Path(str(marker) + ".term").touch())
        marker.touch()
        while True:
            _time.sleep(3600)
    if os.environ.get("GH_TELEMETRY") == "log":
        # Like gh, print the telemetry payload on stderr for every command.
        sys.stderr.write('Telemetry payload: {"command": "api"}\n')

    if "pages" in response:
        pages = response["pages"] if options["paginate"] else response["pages"][:1]
    elif "body" in response:
        pages = [response["body"]]
    else:
        pages = []

    status = 0
    for page in pages:
        status = emit(page, options["jq"]) or status

    code = response.get("status", 200)
    if code >= 400:
        # Like gh: pages fetched before the failing one are already on stdout; the error body follows
        # unfiltered and without a newline; the error line goes to stderr; the exit status is 1.
        if "raw" in response:
            write_stdout(response["raw"])
            sys.stderr.write(f"gh: HTTP {code}\n")
        else:
            message = response.get("message", REASONS.get(code, "Error"))
            write_stdout(json.dumps({"message": message, "status": str(code)}))
            sys.stderr.write(f"gh: {message} (HTTP {code})\n")
            for hint in response.get("hints", []):
                sys.stderr.write(f"gh: {hint}\n")
        return 1

    if "raw" in response:
        if options["jq"] is not None:
            # gh decodes the body before applying --jq and fails on anything that is not JSON.
            first = response["raw"][:1]
            sys.stderr.write(
                f"invalid character {first!r} looking for beginning of value\n" if first else "unexpected end of JSON input\n"
            )
            return 1
        write_stdout(response["raw"])

    if "signal_parent" in response:
        with open(os.environ["FAKE_GH_REQUEST_PID"], "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        os.kill(os.getppid(), response["signal_parent"])
        import time

        time.sleep(30)
    sys.stderr.write(response.get("stderr", ""))
    return response.get("exit", status)


def main(argv: list[str]) -> int:
    with open(os.environ["FAKE_GH_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps(argv) + "\n")
    with open(os.environ["FAKE_GH_FIXTURE"], encoding="utf-8") as handle:
        fixture = json.load(handle)

    if argv == ["auth", "status"]:
        if fixture.get("authenticated", True):
            return 0
        sys.stderr.write("You are not logged into any GitHub hosts.\n")
        return 1

    if argv[:2] == ["repo", "view"]:
        options, _ = parse_options(argv[2:])
        return emit({"viewerPermission": fixture.get("viewer_permission", "ADMIN")}, options["jq"])

    if argv[:1] == ["api"]:
        options, positional = parse_options(argv[1:])
        if len(positional) != 1:
            return unexpected(argv)
        key = f"{options['method']} {positional[0]}"
        responses = fixture.get("api", {}).get(key)
        if responses is None:
            return unexpected(argv)
        return respond(next_response(key, responses), options)

    return unexpected(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
