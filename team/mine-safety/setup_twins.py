#!/usr/bin/env python3
"""Idempotently create or update the mine-safety twins in Eclipse Ditto."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_TWINS_FILE = Path(__file__).with_name("twins.json")
TRANSIENT_HTTP_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504}


def load_twins(path: str | os.PathLike[str] = DEFAULT_TWINS_FILE) -> dict[str, dict]:
    """Load an ordered mapping; JSON insertion order keeps parents before children."""
    with Path(path).open(encoding="utf-8") as source:
        twins = json.load(source)
    if not isinstance(twins, dict) or not twins:
        raise ValueError("twins.json must contain a non-empty object")
    return twins


def authorization_header(user: str, password: str) -> str:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {token}"


def make_put_request(ditto_url: str, auth: str, thing_id: str, body: dict) -> urllib.request.Request:
    encoded_id = urllib.parse.quote(thing_id, safe=":")
    return urllib.request.Request(
        f"{ditto_url.rstrip('/')}/api/2/things/{encoded_id}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="PUT",
        headers={"Authorization": auth, "Content-Type": "application/json; charset=utf-8"},
    )


def put_twin(
    request: urllib.request.Request,
    *,
    attempts: int,
    retry_delay: float,
) -> int:
    """Execute one idempotent PUT, retrying only temporary transport failures."""
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            if exc.code not in TRANSIENT_HTTP_STATUSES or attempt == attempts:
                raise
            print(f"temporary HTTP {exc.code}; retry {attempt}/{attempts}", file=sys.stderr)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == attempts:
                raise
            print(f"temporary connection error; retry {attempt}/{attempts}: {exc}", file=sys.stderr)
        time.sleep(retry_delay)
    raise RuntimeError("unreachable retry state")


def configure_from_environment() -> tuple[str, str, str, Path, int, float]:
    try:
        password = os.environ["DITTO_PASSWORD"]
    except KeyError as exc:
        raise ValueError("DITTO_PASSWORD is required") from exc
    return (
        os.environ.get("DITTO_URL", "http://localhost:8080"),
        os.environ.get("DITTO_USER", "ditto"),
        password,
        Path(os.environ.get("TWINS_FILE", DEFAULT_TWINS_FILE)),
        int(os.environ.get("DITTO_SETUP_ATTEMPTS", "20")),
        float(os.environ.get("DITTO_SETUP_RETRY_DELAY", "3")),
    )


def main() -> int:
    try:
        ditto_url, user, password, twins_file, attempts, retry_delay = configure_from_environment()
        twins = load_twins(twins_file)
        auth = authorization_header(user, password)
        for thing_id, body in twins.items():
            request = make_put_request(ditto_url, auth, thing_id, body)
            status = put_twin(request, attempts=attempts, retry_delay=retry_delay)
            action = "created" if status == 201 else "updated"
            print(f"{action:7} {thing_id}")
        print(f"{len(twins)} mine-safety twins ready in {ditto_url.rstrip('/')}")
        return 0
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.HTTPError, urllib.error.URLError) as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
