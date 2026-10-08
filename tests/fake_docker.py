#!/usr/bin/env python3
"""Offline stand-in for the docker CLI.

Every invocation is appended to FAKE_DOCKER_LOG as one JSON line with its argv, its environment, the content
of every file it was asked to bind-mount, and what it read on standard input with -i, so tests can prove
where a token did and did not go.
Responses come from FAKE_DOCKER_FIXTURE, a JSON object keyed by the subcommand ("wait", "volume create",
...) or by the subcommand and its last argument ("stop gha-x"); the more specific key wins. A response is
{"stdout": "...", "exit": N} and may set "hang": true, which makes the command write FAKE_HANG_MARKER and
block while ignoring TERM, the way a stuck daemon call would; "block": true blocks but answers TERM, like a
command that simply has not returned yet. "wait" blocks until FAKE_DOCKER_STOP_FILE
exists, which "stop" creates, unless the fixture answers it directly.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path


def subcommand(argv: list[str]) -> tuple[str, list[str]]:
    words = [word for word in argv if not word.startswith("-")]
    if words and words[0] in ("volume", "container", "image"):
        return " ".join(words[:2]), words[2:]
    return (words[0] if words else ""), words[1:]


def mounted_files(argv: list[str]) -> dict[str, str]:
    contents = {}
    for index, word in enumerate(argv):
        spec = None
        if word == "--mount" and index + 1 < len(argv):
            spec = argv[index + 1]
        elif word.startswith("--mount="):
            spec = word.split("=", 1)[1]
        if spec is None:
            continue
        fields = dict(part.split("=", 1) for part in spec.split(",") if "=" in part)
        source = fields.get("src") or fields.get("source")
        if source and Path(source).is_file():
            contents[source] = Path(source).read_text(encoding="utf-8")
    return contents


def hang(marker: Path) -> None:
    """Blocks like a stuck daemon call: declares the hang to the fake timeout, and declares TERM when it
    arrives (and ignores it), so the fake timeout can apply the kill grace without measuring real time."""
    signal.signal(signal.SIGTERM, lambda *_: Path(str(marker) + ".term").touch())
    marker.touch()
    while True:
        time.sleep(3600)


def main() -> int:
    argv = sys.argv[1:]
    name, rest = subcommand(argv)
    # What arrived on standard input is recorded too (when it is not a terminal), so tests can prove the
    # token travelled there and nowhere else.
    stdin = ""
    if "-i" in argv and not sys.stdin.isatty():
        stdin = sys.stdin.read()
    with open(os.environ["FAKE_DOCKER_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps({"argv": argv, "env": dict(os.environ), "mounts": mounted_files(argv), "stdin": stdin}) + "\n")
    fixture = {}
    fixture_path = os.environ.get("FAKE_DOCKER_FIXTURE")
    if fixture_path and Path(fixture_path).exists():
        fixture = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    response = None
    if rest:
        response = fixture.get(f"{name} {rest[-1]}")
    if response is None:
        response = fixture.get(name)
    if isinstance(response, list):
        # A list answers successive calls in order; the last entry repeats.
        state_path = Path(os.environ["FAKE_DOCKER_LOG"] + ".calls")
        counts = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        index = counts.get(name, 0)
        counts[name] = index + 1
        state_path.write_text(json.dumps(counts), encoding="utf-8")
        response = response[min(index, len(response) - 1)]
    if response is None:
        response = {}
    if name == "stop":
        Path(os.environ["FAKE_DOCKER_STOP_FILE"]).touch()
    if response.get("hang"):
        hang(Path(os.environ["FAKE_HANG_MARKER"]))
    if response.get("block"):
        # A command that has not returned yet but answers TERM, like docker logs --follow before the line appears.
        sys.stdout.write(response.get("stdout", ""))
        sys.stdout.flush()
        while True:
            time.sleep(3600)
    if name == "wait" and "exit" not in response and "stdout" not in response:
        if os.environ.get("FAKE_DOCKER_WAIT_IGNORES_TERM"):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        stop_file = Path(os.environ["FAKE_DOCKER_STOP_FILE"])
        while not stop_file.exists():
            time.sleep(0.05)
        sys.stdout.write("0\n")
        return 0
    sys.stdout.write(response.get("stdout", ""))
    sys.stderr.write(response.get("stderr", ""))
    return int(response.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main())
