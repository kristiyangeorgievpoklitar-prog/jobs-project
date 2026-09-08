# Operations

Day-to-day running, tuning and recovery.

## Daily use

```bash
uv run jobhunter serve       # dashboard on http://127.0.0.1:8000
```

The dashboard opens on **what should I apply to today?** — the APPLY list, then
the REVIEW list, each with the reason and the biggest risk. Open a job to see
the mandatory and nice-to-have requirements and how each one was judged.

Then tell it what you did. That is not bookkeeping: feedback is the only ground
truth the system ever gets, and it drives what you are shown next.

Terminal equivalents:

```bash
uv run jobhunter today                                # the morning summary
uv run jobhunter evaluate                             # judge anything new
uv run jobhunter feedback 42 apply                    # you applied
uv run jobhunter feedback 42 skip --reason too_senior # and why not
uv run jobhunter preferences                          # what it has learned
uv run jobhunter runs
```

### How long a pass takes

The local model reads one listing at a time and, on a laptop-class GPU, takes
roughly **two minutes per listing**. Two things keep that bounded:

* the Stage 1 gate settles about **40% of listings** without a model call;
* evaluations are cached against the job text, your profile, the model and the
  prompt, so a re-scan only pays for listings that are genuinely new or changed.

A first scan of ~90 listings therefore takes upwards of an hour; the next
morning's scan usually takes minutes. Run it on a schedule overnight rather than
waiting on it.

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

## When the recommendations look wrong

Open the job. The assessment names the requirements it found, marks each one
strong / acceptable / weak / missing / unknown, and says what in your profile it
based that on. That is usually enough to see which of three things went wrong.

**It missed something in your profile.** The model only knows what
`jobhunter profile show` and your CV say. If the CV text is thin, the matching
will be too — this is the single highest-leverage thing to fix.

**It invented something.** Small models sometimes name a technology you do not
have. Give feedback with `--reason technology`; if it recurs on a specific
model, try a different one (`LOCAL_MODEL`) and re-run `jobhunter benchmark` to
confirm the change actually helped.

**It read the posting wrongly.** Re-run that one job with
`jobhunter evaluate --job-id 42 --force`. Decisions are not deterministic across
model versions, but temperature is 0, so the same model and prompt on unchanged
text will repeat itself.

**Too few jobs surfacing**

* Widen `preferred_locations`, or enable remote in your profile.
* Raise `MAX_PAGES_PER_SCAN`.
* Check your skill lists are populated — `jobhunter profile show`.

**Too much noise** — give feedback on the noise. Skips with a reason are what
teach it, and `jobhunter preferences` shows what it has concluded so far.

## Checking it is actually working

```bash
uv run jobhunter benchmark
```

This runs the old scorer, an `always_skip_baseline`, and your configured model
over `evaluation/dataset.json` — 41 real listings labelled by hand. The number
to watch is **worth-surfacing recall**: of the jobs a human said were worth a
look, how many the matcher showed. Plain accuracy is misleading here, because
most listings really are skips and a matcher that refuses everything scores 78%.

If you change `LOCAL_MODEL`, a prompt, or your profile substantially, re-run it.

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
