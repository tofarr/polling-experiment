"""Poll GitHub repositories for new events.

Configuration via environment variables:
    GITHUB_REPOS           Required. Comma-separated 'owner/name' list.
    GITHUB_TOKEN           Optional PAT. Without it, the public API rate
                           limit is 60 requests/hour. With it, 5000/hour.
    STATE_FILE             Path to JSON state file. Default: ./state.json
    POLL_INTERVAL_SECONDS  Lower-bound sleep between polls in --loop mode.
                           The script always also honors GitHub's
                           X-Poll-Interval response header. Default: 60.
    EVENT_TYPES            Optional comma-separated allow-list of event
                           types (e.g. 'PushEvent,PullRequestEvent').
                           Empty/unset = all types.
    MAX_PAGES              Pages of /events to fetch per repo per poll.
                           30 events/page, up to 10 pages. Default: 1.
    OUTPUT_FILE            Optional path; if set, new events are appended
                           as NDJSON in addition to being printed to stdout.
    LOG_LEVEL              Default: INFO.

Usage:
    uv run python scripts/poll_github_events.py           # one-shot
    uv run python scripts/poll_github_events.py --loop    # daemon
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

GITHUB_API = "https://api.github.com"
STATE_VERSION = 1
PER_PAGE = 30
DEFAULT_POLL_INTERVAL = 60
DEFAULT_MAX_PAGES = 1
DEFAULT_STATE_FILE = "state.json"
USER_AGENT = "polling-experiment/0.1"

log = logging.getLogger("poll-github-events")


@dataclass
class Config:
    repos: list[str]
    token: str | None
    state_file: Path
    poll_interval: int
    event_types: set[str] | None
    max_pages: int
    output_file: Path | None

    @classmethod
    def from_env(cls) -> Config:
        repos_raw = os.environ.get("GITHUB_REPOS", "").strip()
        if not repos_raw:
            raise SystemExit(
                "GITHUB_REPOS env var is required (comma-separated 'owner/name')."
            )
        repos = [r.strip() for r in repos_raw.split(",") if r.strip()]
        for r in repos:
            if r.count("/") != 1 or any(part == "" for part in r.split("/")):
                raise SystemExit(f"Invalid repo {r!r}; expected 'owner/name'.")

        event_types_raw = os.environ.get("EVENT_TYPES", "").strip()
        event_types = {t.strip() for t in event_types_raw.split(",") if t.strip()} or None

        output_raw = os.environ.get("OUTPUT_FILE", "").strip()
        output_file = Path(output_raw) if output_raw else None

        max_pages = int(os.environ.get("MAX_PAGES", DEFAULT_MAX_PAGES))
        if not 1 <= max_pages <= 10:
            raise SystemExit("MAX_PAGES must be between 1 and 10 (API hard cap).")

        return cls(
            repos=repos,
            token=os.environ.get("GITHUB_TOKEN") or None,
            state_file=Path(os.environ.get("STATE_FILE", DEFAULT_STATE_FILE)),
            poll_interval=int(
                os.environ.get("POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL)
            ),
            event_types=event_types,
            max_pages=max_pages,
            output_file=output_file,
        )


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": STATE_VERSION, "repos": {}}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f"State file {path} is corrupt: {e}")
    data.setdefault("version", STATE_VERSION)
    data.setdefault("repos", {})
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomic write: write to tmp then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def emit_event(event: dict[str, Any], output_file: Path | None) -> None:
    line = json.dumps(event, separators=(",", ":"))
    print(line, flush=True)
    if output_file:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with output_file.open("a") as f:
            f.write(line + "\n")


def fetch_repo_events(
    client: httpx.Client,
    repo: str,
    etag: str | None,
    max_pages: int,
) -> tuple[int, list[dict[str, Any]], str | None, int]:
    """Fetch events for one repo.

    Returns (status, events, new_etag, poll_interval).

    - On 304, events is [] and new_etag may still be returned (echo of request).
    - Events are returned in API order (newest first).
    - ETag is taken from page 1 only.
    """
    url = f"{GITHUB_API}/repos/{repo}/events"
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = etag

    all_events: list[dict[str, Any]] = []
    new_etag: str | None = None
    poll_interval = 0
    status = 0

    for page in range(1, max_pages + 1):
        params = {"per_page": PER_PAGE, "page": page}
        page_headers = headers if page == 1 else None
        resp = client.get(url, headers=page_headers, params=params)
        status = resp.status_code
        try:
            poll_interval = max(
                poll_interval, int(resp.headers.get("X-Poll-Interval", "0"))
            )
        except ValueError:
            pass

        if page == 1:
            new_etag = resp.headers.get("ETag", new_etag)
            if status == 304:
                return status, [], new_etag, poll_interval

        if status != 200:
            return status, [], new_etag, poll_interval

        page_events = resp.json()
        if not isinstance(page_events, list) or not page_events:
            break
        all_events.extend(page_events)
        if len(page_events) < PER_PAGE:
            break

    return status, all_events, new_etag, poll_interval


def filter_new(
    events: list[dict[str, Any]],
    last_event_id: str | None,
    event_types: set[str] | None,
) -> list[dict[str, Any]]:
    """Return events newer than last_event_id, oldest-first."""
    last_id_int = int(last_event_id) if last_event_id else -1
    new: list[dict[str, Any]] = []
    for e in events:
        try:
            eid = int(e["id"])
        except (KeyError, ValueError, TypeError):
            continue
        if eid <= last_id_int:
            continue
        if event_types and e.get("type") not in event_types:
            continue
        new.append(e)
    new.sort(key=lambda e: int(e["id"]))
    return new


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def poll_repo(
    config: Config,
    state: dict[str, Any],
    client: httpx.Client,
    repo: str,
) -> int:
    """Poll one repo. Returns observed X-Poll-Interval (0 if none)."""
    repo_state: dict[str, Any] = state["repos"].setdefault(repo, {})
    etag = repo_state.get("etag")
    last_event_id = repo_state.get("last_event_id")

    try:
        status, events, new_etag, poll_interval = fetch_repo_events(
            client, repo, etag, config.max_pages
        )
    except httpx.HTTPError as e:
        log.warning("%s: HTTP error: %s", repo, e)
        return 0

    repo_state["last_polled_at"] = _now_iso()
    repo_state["last_status"] = status

    if status == 304:
        log.info("%s: 304 Not Modified", repo)
        if new_etag:
            repo_state["etag"] = new_etag
        return poll_interval
    if status == 404:
        log.warning("%s: 404 (repo not found or no access)", repo)
        return poll_interval
    if status in (401, 403):
        log.warning("%s: %s (auth failure or rate limit)", repo, status)
        return poll_interval
    if status != 200:
        log.warning("%s: unexpected status %s", repo, status)
        return poll_interval

    if new_etag:
        repo_state["etag"] = new_etag

    if last_event_id is None:
        # First sight of this repo: record baseline, do not backfill.
        if events:
            max_id = max(int(e["id"]) for e in events if "id" in e)
            repo_state["last_event_id"] = str(max_id)
            log.info(
                "%s: initial sync, baseline event id %s (%d historical events skipped)",
                repo,
                max_id,
                len(events),
            )
        else:
            log.info("%s: initial sync, no events yet", repo)
        return poll_interval

    new_events = filter_new(events, last_event_id, config.event_types)
    for ev in new_events:
        emit_event(ev, config.output_file)
    if new_events:
        repo_state["last_event_id"] = str(max(int(e["id"]) for e in new_events))
    log.info("%s: %d new event(s)", repo, len(new_events))
    return poll_interval


def poll_once(
    config: Config, state: dict[str, Any], client: httpx.Client
) -> int:
    """Run one polling pass over all repos. Returns sleep interval for --loop."""
    observed = 0
    for repo in config.repos:
        observed = max(observed, poll_repo(config, state, client, repo))
        save_state(config.state_file, state)
    return max(observed, config.poll_interval)


def build_client(token: str | None) -> httpx.Client:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(headers=headers, timeout=30.0)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Poll GitHub repos for new events.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Poll continuously instead of running once.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config = Config.from_env()
    if not config.token:
        log.warning(
            "GITHUB_TOKEN not set; using unauthenticated API (60 req/hour limit)."
        )
    state = load_state(config.state_file)

    with build_client(config.token) as client:
        try:
            while True:
                sleep_for = poll_once(config, state, client)
                if not args.loop:
                    return 0
                log.info("sleeping %ds", sleep_for)
                time.sleep(sleep_for)
        except KeyboardInterrupt:
            log.info("interrupted; state saved")
            return 130


if __name__ == "__main__":
    sys.exit(main())
