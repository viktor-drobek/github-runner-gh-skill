#!/usr/bin/env python3
"""Deterministic stand-in for coreutils timeout, used with the fake clock.

`timeout --kill-after=K D COMMAND...` starts COMMAND in its own session, records D and K in
FAKE_TIMEOUT_LOG, and waits for one of two things: the command exits, and its status is returned with the
fake clock untouched; or the command declares that it hangs by creating FAKE_HANG_MARKER (the fake docker and
the fake gh do so for a scripted hang), or it has run for FAKE_TIMEOUT_FALLBACK_SECONDS of real time (15 by
default) without returning, and then the fake clock advances by D and the command's group gets TERM; a tree that ends on its own within a moment gives status 124, otherwise the clock advances by K, the
group gets KILL, and the status is 137, the sequence and the statuses coreutils timeout produces in real time.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def advance_clock(seconds: int) -> None:
    clock = Path(os.environ["FAKE_CLOCK"])
    clock.write_text(f"{int(clock.read_text().split()[0]) + seconds}\n", encoding="utf-8")


def main() -> int:
    argv = sys.argv[1:]
    kill_after = 0
    while argv and argv[0].startswith("-"):
        option = argv.pop(0)
        if option.startswith("--kill-after="):
            kill_after = int(option.split("=", 1)[1])
        elif option == "-k":
            kill_after = int(argv.pop(0))
    duration = int(argv.pop(0))
    with open(os.environ["FAKE_TIMEOUT_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps({"duration": duration, "kill_after": kill_after, "argv": argv}) + "\n")
    marker = Path(os.environ["FAKE_HANG_MARKER"])
    term_marker = Path(str(marker) + ".term")
    marker.unlink(missing_ok=True)
    term_marker.unlink(missing_ok=True)
    child = subprocess.Popen(argv, start_new_session=True)
    # A command that never declares a hang and never returns is treated as hanging after this much real time,
    # so a test cannot wait forever on a command that does not know the marker protocol.
    fallback_deadline = time.monotonic() + float(os.environ.get("FAKE_TIMEOUT_FALLBACK_SECONDS", "15"))

    def forward(signum, _frame):
        # Like the real timeout: a signal to timeout itself goes to the command's group, then timeout ends.
        try:
            os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    while True:
        status = child.poll()
        if status is not None:
            return status
        if marker.exists() or time.monotonic() > fallback_deadline:
            marker.unlink(missing_ok=True)
            advance_clock(duration)
            os.killpg(child.pid, signal.SIGTERM)
            # As the real timeout: the command's tree gets the kill grace to end on its own after TERM (in real
            # time, capped so tests stay quick); a tree that stays is charged the grace on the fake clock and
            # killed. The status is 124 either way, as the real timeout reports. Tests assert budgets, not exact
            # totals, since the moment the tree ends is not modelled.
            try:
                child.wait(timeout=min(kill_after, 3))
                term_marker.unlink(missing_ok=True)
                return 124
            except subprocess.TimeoutExpired:
                pass
            advance_clock(kill_after)
            os.killpg(child.pid, signal.SIGKILL)
            child.wait()
            term_marker.unlink(missing_ok=True)
            return 137
        time.sleep(0.02)


if __name__ == "__main__":
    sys.exit(main())
