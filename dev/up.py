#!/usr/bin/env python3
"""Brings up the Predbat dev environment (dummy Home Assistant + auto-provisioned
token + Predbat) and opens both web UIs once they're ready.

Usage: dev/up.py   (run from anywhere - it cd's to the repo root for you)
"""

import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = "docker-compose.dev.yml"


def wait_for_url(url, timeout_seconds):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=5)
            return True
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(2)
    return False


def main():
    subprocess.run(["docker", "compose", "-f", COMPOSE_FILE, "up", "-d", "--build"], cwd=REPO_ROOT, check=True)

    print("Waiting for Home Assistant...")
    if not wait_for_url("http://localhost:8123/", 120):
        print("Warn: Home Assistant did not come up within 120s - check: docker compose -f docker-compose.dev.yml logs homeassistant", file=sys.stderr)

    print("Waiting for Predbat...")
    if not wait_for_url("http://localhost:5052/", 180):
        print("Warn: Predbat did not come up within 180s - check: docker compose -f docker-compose.dev.yml logs predbat ha-bootstrap", file=sys.stderr)

    webbrowser.open("http://localhost:8123/")
    webbrowser.open("http://localhost:5052/")

    print("Home Assistant: http://localhost:8123/  (dev / devdevdev1)")
    print("Predbat web UI: http://localhost:5052/")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError:
        print("docker compose up failed", file=sys.stderr)
        sys.exit(1)
