#!/usr/bin/env python3
"""Records every systemd-notify call in FAKE_NOTIFY_LOG, one argument list per line."""
import json
import os
import sys

with open(os.environ["FAKE_NOTIFY_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
