# Architecture

## Design goals

1. **The site contract is isolated.** Everything Jobs.bg-specific — URLs,
   selectors, page structure — lives in `sources/jobsbg/`. A redesign of the
   site is a change in one package.
2. **Pure logic is separable from I/O.** Parsing, normalization, classification
   and scoring are pure functions over data structures. They are tested against
   saved HTML with no browser and no database.
3. **A second job board is a new class, not a rewrite.** Sources implement one
   protocol; nothing downstream knows which board a job came from.
4. **Nothing is silently assumed.** Success is proven by page evidence,
   duplicates are checked on four keys, and a job that was never read cannot be
   auto-applied to.

## Stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.12 | Best browser-automation and parsing ecosystem. |
| Browser | Playwright (Chromium, headed) | Reliable waits, file upload, persistent profiles. |
| Persistence | SQLite + SQLAlchemy 2.0 | Local, zero-setup, transactional. |
| Migrations | Alembic | Schema history from day one. |
| Web | FastAPI + Jinja2 | Async server, server-rendered pages, no build step. |
| Config | pydantic-settings | Typed, validated, env-driven. |
| Logging | structlog | Structured events with automatic secret redaction. |
| Scheduling | APScheduler | Cron and interval triggers in-process. |
| CLI | Typer + Rich | Readable terminal output. |
| Tests | pytest | 343 tests, 82% coverage, no network. |

## Module map

```
src/jobhunter/
├── config.py              Typed settings from .env
├── settings_store.py      Runtime overrides (dashboard-editable, validated)
├── context.py             Wires db + AI provider + notifier + engine
├── logging_setup.py       structlog + secret redaction
├── cli.py                 Typer entry point
│
├── domain/                enums.py, schemas.py — the shared vocabulary
├── db/                    base.py (engine, StrEnumType), models.py, migrations/
│
├── sources/
│   ├── base.py            JobSource protocol — the extension point
│   └── jobsbg/
│       ├── urls.py        URL building, location ids, canonicalisation
│       ├── selectors.py   Every selector and site literal
│       ├── parser.py      HTML -> RawJob (pure, fixture-tested)
│       ├── discovery.py   Paginated crawl
│       └── applier.py     Application form automation
│
├── browser/
│   ├── manager.py         Playwright lifecycle, pacing, robots.txt, retries
│   ├── challenge.py       Anti-bot detection (pure function)
│   └── diagnostics.py     Screenshots + scrubbed HTML dumps
│
├── normalize/normalizer.py    RawJob -> NormalizedJob, fingerprints
├── classify/                  keywords.py (BG+EN tables), classifier.py
├── matching/
│   ├── gate.py            Stage 1: reject only what no reading could rescue
│   ├── job_context.py     Posting -> the text the model reads; content hash
│   ├── evaluator.py       Stage 1 -> cache -> model -> policy
│   ├── policy.py          Safety rules; only ever downgrade a decision
│   ├── rules.py           The old deterministic scorer (fallback + benchmark)
│   └── engine.py          Classification wrapper, still used for structure
├── prompts/
│   ├── __init__.py        Versioned loader; identity = name.version.checksum
│   └── templates/         job_evaluation.v1/v2/v3.{system,user}.txt
├── ai/                        local_model.py (Ollama) + rule_based/anthropic/openai
├── personalization/           learner.py (preferences), ranker.py (ordering)
├── evaluation/                dataset.py, metrics.py, runner.py, build.py
├── briefing.py                The daily "what should I apply to today"
├── today.py                   Today's Jobs: the date-bounded daily pass
├── profile/                   cv.py, profile_store.py, context.py (candidate text)
├── applications/              state_machine.py, dedupe.py, orchestrator.py, cover_letter.py
├── notifications/             console, dashboard, telegram, manager
├── pipeline/                  runner.py, repository.py, evaluation_store.py,
│                              feedback_store.py
├── scheduler/scheduler.py
└── web/                       app.py, deps.py, routes/, templates/, static/
```

## The scan pipeline

```
 discovery.search()          paginated, polite, challenge-aware
        │  list[RawJob]  (listing cards: title, company, level, years, date)
        ▼
 _prioritise_for_enrichment  drop what the Stage 1 gate rejects; keep site order
        │                    (deliberately NOT ranked by a provisional score —
        │                     a card has no requirements text, so such a ranking
        │                     orders listings by how little is known about them)
        ▼
 discovery.enrich()          fetch the top N detail pages (bounded)
        │  read_description_frame()  the posting body lives in a sandboxed
        │  merge_detail()            iframe, not the page DOM
        ▼
 normalize_job()             clean, parse salary/date/experience, fingerprint
        ▼
 upsert_job()                insert, or update last_seen/seen_count (idempotent)
        ▼
 classify_job()              structure only: seniority tag, language, bullets.
        │                    No longer decides anything.
        ▼
 JobEvaluator.evaluate()
        ├─ 1. Stage1Gate      certain rejects, no model call            ~43% of jobs
        ├─ 2. cache lookup    same job + profile + model + prompt       free
        ├─ 3. local model     reads the posting and the CV               ~99s/job
        └─ 4. apply_policy    downgrade what the evidence cannot carry
        ▼
 transition_job()            CLASSIFIED -> MATCHED -> REVIEW/APPROVED/SKIPPED
        ▼
 notifier.high_match_job()
```

Each job is processed in its own transaction. One failure is recorded as an
`ErrorRecord` and the run continues.

### The today-scan

`today.py` runs the same pipeline with one bound: `ScanOptions.posted_on`. It
changes two things and nothing else.

**The site does the date filtering.** `ScanOptions.posted_on` is mapped to the
`last` parameter behind the site's own "Публикувани" chip — the control the
candidate clicks by hand — so a search for today returns today's listings and
nothing else. The card date is still re-checked locally, because the wider
windows are cumulative rather than a single day.

**One digest replaces the per-job notifications.** `suppress_notifications`
turns off the per-listing alert and the "scan completed" message; the caller
sends a single summary instead, and only when the run found listings the
database had never recorded. Challenge and failure notifications are never
suppressed.

Three questions are answered here and they are deliberately not the same:

| | means | decides |
|---|---|---|
| published today | the card printed today's date | whether it is part of today's set |
| new to this run | `upsert_job` inserted the row | whether it is **announced** |
| discovered today | `first_seen_at` falls in today | how the **page badges** it |

The last two differ on purpose. The digest must not repeat itself, so it asks
the narrow question — did *this* run find it — and a listing a full scan stored
an hour ago is not announced again. The page outlives any one run, so it asks
the wider one; badging everything "already seen" after the day's second scan
would erase the distinction the page exists to show.

A listing published today and seen by an earlier scan today is shown, marked
*already seen* by the command, and never announced twice. That is what makes running the
command a second time cost nothing: the gate settles what it settled before,
the evaluation cache answers for every unchanged listing, and no notification
is sent.

The two dates are also read differently, which is easy to get wrong. A card
date has no time, so `parse_posted_date` stores it as midnight **UTC** and the
"published today" filter follows that convention. `first_seen_at` is a real
timestamp, so "discovered today" is the **local** day converted to UTC — in
Bulgaria, midnight UTC falls in the middle of the previous evening.

### Why two stages

A local model costs about 100 seconds per listing on the target hardware, so
most listings have to be settled without it. The split follows one rule: **a
gate rejection is final and invisible**, so the gate may only reject on grounds
that are certain from the listing alone — an unambiguously senior title, a role
that is not software, something the candidate already turned down.

The gate explicitly does *not* look at technology overlap. That is the judgement
that most needs a reader, and it is where the previous scorer failed worst.

### Why the score is gone from the decision path

The old scorer returned 0–100 across weighted components. Measured against the
labelled set it reached 83% decision accuracy — against 78% for a stub that
skips every listing. It found none of the strong matches, because its technology
component returned a neutral 0.5 when it detected *no* technologies and a
proportional score when it detected some. Parsing failure therefore outranked a
genuine partial match, and jobs whose requirements it could not read floated to
the top.

A score also has no failure mode: it cannot distinguish "confidently a poor
fit" from "we could not tell", and it cannot say which requirement is missing.
The replacement is a set of separately checkable claims — see
`domain/evaluation.py`.

## The Jobs.bg contract

Derived by driving the live site during development. Recorded here because none
of it is documented publicly.

**Search** — `https://www.jobs.bg/front_job_search.php`

| Parameter | Meaning |
|---|---|
| `subm=1` | Submit marker, always present. |
| `categories[]=56` | The aggregated "IT JOBS" section. |
| `location_sid=N` | City. Verified: Sofia 1, Plovdiv 2, **Varna 3**, Burgas 4, … |
| `is_entry_level=1` | The site's own entry-level filter. |
| `page=N` | **Pagination, 1-indexed, 20 results per page.** |
| `last=N` | **The "Публикувани" date filter; 2 is today.** See below. |

### The publication filter

The site's own "Публикувани днес" is a URL parameter, `last`, and finding it
mattered more than anything else in this feature. It is not in the query string
after clicking — the chip posts the form — but the filter sheet is in the
rendered HTML, and each option is a checkbox named `last`:

| `last` | Option | Covers |
|---|---|---|
| 2 | Днес | today only |
| 3 | Вчера | yesterday only |
| 4 | Последните 3 дни | today back 2 days |
| 5 | Последните 7 дни | today back 6 days |
| 6 | Последните 14 дни | today back 13 days |

2 and 3 are single days; 4-6 are cumulative windows ending today, so a day
picked out of one still has to be sieved by its card date. Nothing older than a
fortnight can be expressed, and `published_window` returns `None` there — the
crawl then reads unfiltered pages and filters locally.

**Results are not ordered by date, and assuming they were was a real bug.** The
first version of this feature paged until it hit a page with nothing from the
target date, on the theory that pages run newest first. Measured live on
2026-09-09, IT/Varna: page 1 opened with 02.09, ran down to 18.08, then began a
second block whose first card — `вчера` — was the newest on the page. The
`last=2` search reported exactly **one** listing that day, and it was not on
page 1 at all, so the early stop reported a quiet day and hid it. Exhaustion is
now judged by a page repeating listing ids already seen, never by the date
sieve.

Pagination was the one genuinely non-obvious part. The page looks like an
infinite scroll, but scrolling loads nothing — it reaches the exact bottom and
stops. `frompage`, `start`, `offset` and `p` are all accepted and silently
ignored, returning page 1 every time. Only `page=N` shifts the window, which was
confirmed by checking that consecutive pages share zero listing ids.

**Listing card** — `div.mdc-layout-grid__inner` containing `a[href*="/job/"]`

* `.card-date` — `DD.MM.YY`, **or `днес` / `вчера`** for the last two days
  (`онзи ден` also appears). This is not a cosmetic detail: measured live, an
  IT search over Sofia returned twenty cards on page 1 reading *only* those
  words and not one numeric date, so a parser that understands `DD.MM.YY` alone
  cannot see a single listing published today. `parse_posted_date` resolves the
  words against the real current date — the site wrote them when the page was
  fetched, so asking for an earlier day must still read `вчера` as yesterday.
* `.scroll-area[data-id]` — the listing id
* `.card-info` — `Месторабота: X; Ниво …; Години опит от N до M; Отпуск …`.
  Listings in the searched city omit the `Месторабота:` label and lead with the
  bare city name, which the parser handles.
* `.skill` — technology tags
* `[data-action="searchSubscribe"]` — a JSON blob carrying company name and id

**Detail page** — `#jobViewContent`

* `.view-extra span.bold` — title, followed by `, Company`
* `.options li` — icon-keyed fields: `location_on`, `stairs` (level),
  `psychology` (years), `chair` (home office), `work`, `schedule`,
  `beach_access`, `language`, `payments`
* `ul li` without an icon — technology tags
* Apply route: `a[href*="js_send_cv"]` (internal) or the text
  `ВЪНШНО КАНДИДАТСТВАНЕ` plus an off-site link (external)

**Application** — two internal routes exist: `js_send_cv.php` (requires a
signed-in account) and `js_send_cv_regless.php` ("apply without an account").
The system prefers the latter when no session is present. Either can redirect to
`js_fill_questionary.php` with employer-defined questions. The submit button
calls `submitRecaptchaForm()`.

## Key decisions

**Seniority means the entry bar, not the ceiling.** A listing tagged
"Mid-level, Senior-level" is classified `mid`, because mid is the level you must
reach to be eligible. Ranges therefore resolve to their minimum — with one
exception: an explicit senior or lead word in the *title* overrides a lower tag,
so a senior posting is never under-called.

**The evidence gate.** A job whose description was never fetched has been judged
on its title and card metadata alone. That is enough to shortlist it, never
enough to recommend applying unseen, so its score is capped below
`AUTO_APPLY_THRESHOLD` and its confidence halved. This is why enrichment is
prioritised by provisional score rather than list order.

**Duplicate prevention is layered.** In order of strength: the site's listing id
→ the canonical URL → company + title (only when no listing id exists). On top
of that, `applications` has a unique constraint on `job_id`, `JobState.APPLIED`
is terminal in the state machine, and `has_applied()` additionally blocks
applying to the same company+title through a different listing.

**AI never weakens a safety rule.** A model can raise or lower a score, but
disqualifiers found by the deterministic rules are merged back in and always
force `SKIP`. A model cannot talk the system into applying to a senior role in
the wrong city.

**Sessions are warmed up.** The first request of a run goes to the site root
before any search URL. A cold profile that deep-links into
`front_job_search.php` receives an empty 403; landing on the home page first
establishes the ordinary session. This surfaced only when the first-run path was
tested end to end from a clean profile, and it had blocked every first run.

**A status code alone never means blocked.** Cloudflare answers a cold profile
with 403 plus a challenge page that then clears itself, so by the time the page
is inspected the real content is present. `detect_challenge` therefore only
treats 403/429 as blocking when the rendered page is also empty; explicit text
markers still count on their own.

**Enums round-trip properly.** A `StrEnumType` decorator converts back to the
enum on load, so `Mapped[JobState]` really does contain a `JobState` rather than
a bare string.

## Data model

`companies` · `jobs` · `job_matches` (append-only score history) ·
`candidate_profile` (versioned) · `cv_files` · `applications` (unique per job) ·
`application_events` (append-only audit trail) · `settings` · `automation_runs` ·
`errors` · `notifications`

## State machine

```
DISCOVERED → CLASSIFIED → MATCHED → REVIEW ⇄ APPROVED → APPLYING → APPLIED
                                       ↘        ↓            ↓
                                     SKIPPED  BLOCKED ⇄   FAILED
```

`APPLIED` is terminal. Every transition is validated and written to
`application_events` with its from/to states.

## Extending to another job board

1. Add `sources/<board>/` with `urls.py`, `selectors.py`, `parser.py`,
   `discovery.py`.
2. Implement the `JobSource` protocol (`search`, `fetch_detail`).
3. Register it in the pipeline.

Normalization, classification, matching, dedupe, the state machine, the
dashboard and the scheduler are all source-agnostic and need no changes.
