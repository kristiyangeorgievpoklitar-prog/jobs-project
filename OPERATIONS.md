# Operations

Day-to-day running, tuning and recovery.

## Daily use

```bash
uv run jobhunter serve       # dashboard on http://127.0.0.1:8000
```

Work the **Pending review** queue: open a job, read the match analysis, then
**Approve**, **Skip**, or **Prepare application**.

Terminal equivalents:

```bash
uv run jobhunter jobs --state review --min-score 75
uv run jobhunter runs
uv run jobhunter applications
```

## Scheduling

```bash
uv run jobhunter schedule
```

Runs daily at `SCAN_AT_HOUR`. Overlapping runs are coalesced and only one may be
in flight, so a slow scan can never pile up.

As a systemd user service (`~/.config/systemd/user/jobhunter.service`):

```ini
[Unit]
Description=JobHunter scheduler
After=graphical-session.target

[Service]
Type=simple
WorkingDirectory=%h/Desktop/jobs project
ExecStart=%h/.local/bin/uv run jobhunter schedule
Restart=on-failure
Environment=DISPLAY=:0

[Install]
WantedBy=default.target
```

```bash
systemctl --user enable --now jobhunter
journalctl --user -u jobhunter -f
```

`DISPLAY` matters: the browser runs headed, so the service needs a desktop
session. Do not run this on a headless server without a virtual display.

## Tuning the match rate

**Too few jobs surfacing**

* Lower `REVIEW_THRESHOLD` (75 → 65) in Settings.
* Raise `MAX_SENIORITY` from `mid` to `mid_senior`.
* Widen `preferred_locations`, or enable remote in your profile.
* Raise `MAX_PAGES_PER_SCAN`.
* Check your skill lists are populated — `jobhunter profile show`.

**Too much noise**

* Raise `REVIEW_THRESHOLD`.
* Lower `MAX_SENIORITY` to `junior_mid`.
* Turn off remote if you only want local roles.

**Scores look wrong on a specific job** — open it in the dashboard. The
component scores show exactly which dimension drove the result, and the
reasoning line explains each one.

## Enabling automatic applications

The default is review-only. Before turning it on:

1. Run scans for a few days and check the REVIEW queue matches your judgement.
2. Prepare a few applications manually (`jobhunter apply <id>`) and inspect the
   filled forms in `screenshots/`.
3. Read the generated cover letters. They are template-built unless a cloud AI
   is configured.

Then set `AUTO_APPLY=true` (Settings, or `.env`) and keep
`MAX_AUTO_APPLICATIONS_PER_RUN` low (3–5).

Even then, most Jobs.bg submissions still need you: reCAPTCHA and employer
questionnaires both stop the automation by design.

## Monitoring

| Where | What |
|---|---|
| Dashboard → Overview | Discovered, relevant, high-match, pending, blocked, failed |
| Dashboard → Notifications | High matches, blocks, challenges, failures |
| `logs/jobhunter.log` | Rotating JSON log, 5 MB × 5 |
| `screenshots/` | Failure and application screenshots plus scrubbed HTML |
| `jobhunter runs` | Per-run counters and status |

The `errors` table records every failure with category, message and traceback.

## Recovery

**Run marked BLOCKED** — a challenge appeared. Complete it in the open browser
window and re-run. If it repeats, raise `REQUEST_DELAY_SECONDS` and scan less
often. Never lower the delay to compensate.

**Login wall on apply** — sign into Jobs.bg once in the browser window. The
session persists in `data/browser-profile/`.

**Browser will not start** — `uv run playwright install chromium`, then
`jobhunter doctor`.

**A job stuck in APPLYING** — a crash mid-application. Re-running `apply` moves
it on; the state machine permits `APPLYING → FAILED/BLOCKED/APPLIED` only.

**Reset the browser session** — delete `data/browser-profile/`. You will need to
log in and re-accept cookies.

**Start over completely**

```bash
rm -f data/jobhunter.db* && uv run jobhunter init
```

This deletes your application history, which is what prevents re-applying to
jobs you have already applied to. Back it up first:

```bash
cp data/jobhunter.db data/jobhunter.db.backup
```

## Backups

`data/jobhunter.db` is the only stateful file that matters. It contains personal
data (profile, CV text, application history) and is excluded from git.

```bash
sqlite3 data/jobhunter.db ".backup data/backup-$(date +%F).db"
```

## Being a good citizen

* Keep `REQUEST_DELAY_SECONDS` at 3s or more.
* Scan once a day; the board does not change faster than that.
* Keep `MAX_PAGES_PER_SCAN` at what you will actually read.
* Never apply to a job you would not genuinely take.

A daily scan of 5 pages plus 45 detail pages is roughly 50 requests spread over
several minutes — comparable to a person browsing the site.

## Upgrading

```bash
uv sync --extra ai
uv run alembic upgrade head
make check
```
