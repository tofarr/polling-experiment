"""Poll Slack channels for new messages.

Uses `conversations.history` with `oldest=<last_ts>` as the high-water mark.
A Slack user token (`xoxp-...`) is recommended; bot tokens (`xoxb-...`) also
work but the bot must be a member of each channel.

Configuration via environment variables:
    SLACK_TOKEN            Required. xoxp-... (user) or xoxb-... (bot).
    SLACK_CHANNELS         Required. Comma-separated channel IDs
                           (e.g. 'C0123456,C0234567'). IDs are stable across
                           renames; names are not. Find an ID by opening the
                           channel in Slack and clicking the channel name —
                           the ID is at the bottom of the "About" panel.
    STATE_FILE             Path to JSON state file. Default: ./slack_state.json
    POLL_INTERVAL_SECONDS  Lower-bound sleep between polls in --loop mode. The
                           script always also honors Retry-After on 429.
                           Default: 60.
    MAX_PAGES              Pages of history to fetch per channel per poll
                           (200 messages/page). Default: 1. Raise this if a
                           channel routinely produces >200 messages per
                           polling interval, otherwise some messages will be
                           skipped to keep the high-water mark advancing.
    OUTPUT_FILE            Optional path; if set, new messages are appended
                           as NDJSON in addition to being printed to stdout.
    LOG_LEVEL              Default: INFO.

Required OAuth scopes (depending on the channel types you poll):
    channels:history, groups:history, im:history, mpim:history

Usage:
    uv run python scripts/poll_slack_messages.py           # one-shot
    uv run python scripts/poll_slack_messages.py --loop    # daemon
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

SLACK_API = "https://slack.com/api"
STATE_VERSION = 1
PER_PAGE = 200
DEFAULT_POLL_INTERVAL = 60
DEFAULT_MAX_PAGES = 1
DEFAULT_STATE_FILE = "slack_state.json"
USER_AGENT = "polling-experiment/0.1"
CHANNEL_ID_RE = re.compile(r"^[A-Z][A-Z0-9]+$")

log = logging.getLogger("poll-slack-messages")


@dataclass
class Config:
    token: str
    channels: list[str]
    state_file: Path
    poll_interval: int
    max_pages: int
    output_file: Path | None

    @classmethod
    def from_env(cls) -> Config:
        token = os.environ.get("SLACK_TOKEN", "").strip()
        if not token:
            raise SystemExit(
                "SLACK_TOKEN env var is required (xoxp-... user or xoxb-... bot token)."
            )

        channels_raw = os.environ.get("SLACK_CHANNELS", "").strip()
        if not channels_raw:
            raise SystemExit(
                "SLACK_CHANNELS env var is required "
                "(comma-separated channel IDs like 'C0123456')."
            )
        channels = [c.strip() for c in channels_raw.split(",") if c.strip()]
        for c in channels:
            if not CHANNEL_ID_RE.match(c):
                raise SystemExit(
                    f"Invalid channel id {c!r}; expected uppercase alphanumeric "
                    "like 'C0123456' (use channel IDs, not names)."
                )

        output_raw = os.environ.get("OUTPUT_FILE", "").strip()
        output_file = Path(output_raw) if output_raw else None

        max_pages = int(os.environ.get("MAX_PAGES", DEFAULT_MAX_PAGES))
        if max_pages < 1:
            raise SystemExit("MAX_PAGES must be >= 1.")

        return cls(
            token=token,
            channels=channels,
            state_file=Path(os.environ.get("STATE_FILE", DEFAULT_STATE_FILE)),
            poll_interval=int(
                os.environ.get("POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL)
            ),
            max_pages=max_pages,
            output_file=output_file,
        )


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": STATE_VERSION, "channels": {}}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f"State file {path} is corrupt: {e}")
    data.setdefault("version", STATE_VERSION)
    data.setdefault("channels", {})
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Atomic write: write to tmp then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def emit_message(
    channel: str, message: dict[str, Any], output_file: Path | None
) -> None:
    # Slack's message objects don't include the channel id; inject it for context.
    record = {"channel": channel, **message}
    line = json.dumps(record, separators=(",", ":"))
    print(line, flush=True)
    if output_file:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with output_file.open("a") as f:
            f.write(line + "\n")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _slack_get(
    client: httpx.Client,
    method: str,
    params: dict[str, Any],
) -> tuple[int, dict[str, Any], int]:
    """GET a Slack Web API method.

    Returns (http_status, body, retry_after).

    Slack returns HTTP 200 even for application errors; callers must check
    body["ok"]. HTTP 429 means rate-limited and `retry_after` is set from
    the response header.
    """
    url = f"{SLACK_API}/{method}"
    resp = client.get(url, params=params)
    status = resp.status_code
    if status == 429:
        try:
            retry_after = int(resp.headers.get("Retry-After", "1"))
        except ValueError:
            retry_after = 1
        return status, {}, max(retry_after, 1)
    try:
        body = resp.json()
    except ValueError:
        body = {"ok": False, "error": f"non_json_response_status_{status}"}
    return status, body, 0


def fetch_latest_ts(client: httpx.Client, channel: str) -> tuple[str | None, str | None]:
    """Return (latest_ts, error). latest_ts is None if channel is empty or error."""
    _, body, _ = _slack_get(
        client, "conversations.history", {"channel": channel, "limit": 1}
    )
    if not body.get("ok"):
        return None, body.get("error", "unknown_error")
    msgs = body.get("messages") or []
    return (msgs[0].get("ts") if msgs else None), None


def fetch_new_messages(
    client: httpx.Client,
    channel: str,
    oldest: str,
    max_pages: int,
) -> tuple[bool, list[dict[str, Any]], bool, str | None, int]:
    """Fetch messages newer than `oldest` (exclusive).

    Returns (ok, messages, has_more_remaining, error, retry_after).

    `has_more_remaining` is True iff we stopped paginating because of
    `max_pages` while Slack still indicated more messages were available.
    Messages are returned in Slack's native order (newest-first).
    """
    all_msgs: list[dict[str, Any]] = []
    cursor: str | None = None
    has_more = False
    for _ in range(max_pages):
        params: dict[str, Any] = {
            "channel": channel,
            "oldest": oldest,
            "limit": PER_PAGE,
        }
        if cursor:
            params["cursor"] = cursor
        status, body, retry_after = _slack_get(
            client, "conversations.history", params
        )
        if status == 429:
            return False, all_msgs, False, "rate_limited", retry_after
        if not body.get("ok"):
            return False, all_msgs, False, body.get("error", "unknown_error"), 0
        all_msgs.extend(body.get("messages") or [])
        has_more = bool(body.get("has_more"))
        cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
        if not has_more or not cursor:
            return True, all_msgs, False, None, 0
    # Hit max_pages with has_more still true.
    return True, all_msgs, has_more, None, 0


def poll_channel(
    config: Config,
    state: dict[str, Any],
    client: httpx.Client,
    channel: str,
) -> int:
    """Poll one channel. Returns Retry-After seconds (0 if none)."""
    chan_state: dict[str, Any] = state["channels"].setdefault(channel, {})
    last_ts = chan_state.get("last_ts")
    chan_state["last_polled_at"] = _now_iso()

    try:
        if last_ts is None:
            # First sight: record current high-water mark, no backfill.
            latest, err = fetch_latest_ts(client, channel)
            if err:
                chan_state["last_status"] = err
                log.warning("%s: %s", channel, err)
                return 0
            if latest is None:
                chan_state["last_status"] = "baseline_empty"
                log.info("%s: initial sync, channel has no messages yet", channel)
                return 0
            chan_state["last_ts"] = latest
            chan_state["last_status"] = "ok"
            log.info("%s: initial sync, baseline ts %s", channel, latest)
            return 0

        ok, msgs, truncated, err, retry_after = fetch_new_messages(
            client, channel, last_ts, config.max_pages
        )
    except httpx.HTTPError as e:
        chan_state["last_status"] = "http_error"
        log.warning("%s: HTTP error: %s", channel, e)
        return 0

    if not ok:
        chan_state["last_status"] = err or "error"
        suffix = f" (retry after {retry_after}s)" if retry_after else ""
        log.warning("%s: %s%s", channel, err, suffix)
        return retry_after

    # Slack returns newest-first; emit oldest-first for natural chronological order.
    msgs.sort(key=lambda m: float(m.get("ts", "0")))
    for m in msgs:
        emit_message(channel, m, config.output_file)
    if msgs:
        chan_state["last_ts"] = msgs[-1]["ts"]
    chan_state["last_status"] = "ok"

    if truncated:
        log.warning(
            "%s: fetched %d message(s) but more remain; advancing last_ts past "
            "this batch — older messages within the gap will be skipped. "
            "Raise MAX_PAGES to catch up.",
            channel,
            len(msgs),
        )
    else:
        log.info("%s: %d new message(s)", channel, len(msgs))
    return 0


def poll_once(
    config: Config, state: dict[str, Any], client: httpx.Client
) -> int:
    """Run one polling pass over all channels. Returns the sleep interval."""
    retry_after = 0
    for channel in config.channels:
        retry_after = max(retry_after, poll_channel(config, state, client, channel))
        save_state(config.state_file, state)
    return max(retry_after, config.poll_interval)


def build_client(token: str) -> httpx.Client:
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    return httpx.Client(headers=headers, timeout=30.0)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Poll Slack channels for new messages.",
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
