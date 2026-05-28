# polling-experiment

A scratch space for experimental Python scripts, managed with [uv](https://docs.astral.sh/uv/).

## Requirements

- [uv](https://docs.astral.sh/uv/getting-started/installation/) (0.4+)
- Python 3.13 (uv will install it automatically if missing; pinned via `.python-version`)

## Setup

Clone the repo and sync the environment:

```bash
uv sync
```

This creates a `.venv/` and installs everything pinned in `uv.lock`.

## Configuration

Each script is configured entirely via environment variables (full
tables under [Scripts](#scripts)). A `.env.template` file at the
repository root lists every variable both scripts understand, with
inline comments.

To set up locally:

```bash
cp .env.template .env
$EDITOR .env          # fill in tokens, repos, channels
```

`.env` is gitignored — `.env.template` is the only env file that
should ever be committed. **Don't put real secrets in `.env.template`.**

Pass `.env` to the scripts using uv's built-in `--env-file`:

```bash
uv run --env-file .env python scripts/poll_github_events.py
uv run --env-file .env python scripts/poll_slack_messages.py
```

If you'd rather load `.env` into your current shell:

```bash
set -a; source .env; set +a
uv run python scripts/poll_github_events.py
```

> **Note on `STATE_FILE`:** the variable name is shared between
> scripts, so if you run both from one `.env` give each its own
> file on the command line:
> ```bash
> uv run --env-file .env env STATE_FILE=state.json \
>   python scripts/poll_github_events.py
> uv run --env-file .env env STATE_FILE=slack_state.json \
>   python scripts/poll_slack_messages.py
> ```
> Or leave `STATE_FILE` unset in `.env` and rely on each script's
> default (`state.json` and `slack_state.json` respectively).

## Running scripts

Experimental scripts live in [`scripts/`](./scripts). Run them with `uv run` so
they execute inside the project's virtualenv:

```bash
uv run python scripts/poll_github_events.py
```

## Adding dependencies

```bash
# Runtime dependency
uv add httpx

# Dev-only dependency
uv add --dev pytest ruff
```

Both `pyproject.toml` and `uv.lock` will be updated; commit both.

## Layout

```
.
├── .env.template       # Copy to .env and fill in values
├── pyproject.toml      # Project metadata & dependencies
├── uv.lock             # Fully-resolved lockfile (commit this)
├── .python-version     # Pinned Python version for uv
├── scripts/            # Experimental scripts
│   ├── poll_github_events.py
│   └── poll_slack_messages.py
└── README.md
```

## Scripts

### `scripts/poll_github_events.py`

Polls one or more GitHub repositories for new events. Uses
`last_event_id` for "what's new?" filtering and `ETag` /
`If-None-Match` so idle polls cost no rate limit. State (per-repo
`last_event_id`, `etag`, `last_polled_at`, `last_status`) is persisted
to a JSON file via atomic write.

Configuration is entirely via environment variables:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `GITHUB_REPOS` | ✓ | — | Comma-separated `owner/name` list. |
| `GITHUB_TOKEN` |  | unset | PAT or fine-grained token. Strongly recommended (60 req/hr → 5000 req/hr). |
| `STATE_FILE` |  | `./state.json` | Where to persist polling state. |
| `POLL_INTERVAL_SECONDS` |  | `60` | Lower bound on sleep in `--loop` mode; `X-Poll-Interval` is also honored. |
| `EVENT_TYPES` |  | all | Comma-separated allow-list (e.g. `PushEvent,PullRequestEvent`). |
| `MAX_PAGES` |  | `1` | Pages of 30 events to fetch per repo per poll (max 10 = 300 events). |
| `OUTPUT_FILE` |  | unset | If set, new events are appended as NDJSON in addition to stdout. |
| `LOG_LEVEL` |  | `INFO` | Standard Python log level. |

**One-shot:**

```bash
GITHUB_REPOS="octocat/Hello-World,github/docs" \
GITHUB_TOKEN=ghp_... \
uv run python scripts/poll_github_events.py
```

**Loop (daemon):**

```bash
GITHUB_REPOS="octocat/Hello-World" \
GITHUB_TOKEN=ghp_... \
uv run python scripts/poll_github_events.py --loop
```

**First-run behavior:** when a repo has no prior state, the script
records the current max event id as the baseline and emits **no**
historical events. New events appear on subsequent polls. To force a
backfill, delete the repo's entry from `state.json` and set
`MAX_PAGES=10`, then manually edit `last_event_id` to a lower value (or
remove it and accept the baseline behavior on the *next* run).

**Output:** each new event is printed as one compact JSON object per
line (NDJSON) to stdout, oldest-first within a poll.

### `scripts/poll_slack_messages.py`

Polls one or more Slack channels for new messages via
`conversations.history`, using the message `ts` as a per-channel
high-water mark. State (per-channel `last_ts`, `last_polled_at`,
`last_status`) is persisted to a JSON file via atomic write.

Slack does **not** expose ETag-style conditional requests, but the
API filters server-side via `oldest=<last_ts>` so you only ever
receive truly new messages.

Configuration is entirely via environment variables:

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `SLACK_TOKEN` | ✓ | — | `xoxp-…` user token (recommended) or `xoxb-…` bot token. |
| `SLACK_CHANNELS` | ✓ | — | Comma-separated channel IDs (e.g. `C0123456`). IDs, not names. |
| `STATE_FILE` |  | `./slack_state.json` | Where to persist polling state. |
| `POLL_INTERVAL_SECONDS` |  | `60` | Lower bound on sleep in `--loop` mode; `Retry-After` on 429 is also honored. |
| `MAX_PAGES` |  | `1` | Pages of 200 messages per channel per poll. Raise if a channel routinely produces >200 messages per interval. |
| `OUTPUT_FILE` |  | unset | If set, new messages are appended as NDJSON in addition to stdout. |
| `LOG_LEVEL` |  | `INFO` | Standard Python log level. |

**Required OAuth scopes** (on the Slack App; choose those matching the channel types you poll):

- `channels:history` — public channels
- `groups:history` — private channels
- `im:history` — DMs
- `mpim:history` — group DMs

**Finding channel IDs:** in Slack, open the channel → click its
name → scroll the "About" panel to the bottom; the ID (e.g.
`C0123456`) is shown there.

**One-shot:**

```bash
SLACK_TOKEN=xoxp-... \
SLACK_CHANNELS="C0123456,C0234567" \
uv run python scripts/poll_slack_messages.py
```

**Loop (daemon):**

```bash
SLACK_TOKEN=xoxp-... \
SLACK_CHANNELS="C0123456" \
uv run python scripts/poll_slack_messages.py --loop
```

**First-run behavior:** when a channel has no prior state, the
script records the latest message `ts` as the baseline and emits
**no** historical messages — same convention as the GitHub script.

**Output:** each new message is printed as one compact JSON object
per line (NDJSON) to stdout, oldest-first within a poll. Each
record has an added top-level `channel` field (the channel ID)
because Slack's message objects don't include it.

**Edits and deletes** are *not* surfaced — `conversations.history`
returns messages by their original `ts` and won't re-emit edited
ones. For real-time edit/delete events, use the Slack Events API
or Socket Mode (different architecture).
