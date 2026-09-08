# JobHunter

A personal, local-first job-hunting agent for **[Jobs.bg](https://www.jobs.bg/)**.

It discovers IT listings in your target city, works out whether each one is
genuinely an IT role at a seniority you could actually get, scores it against
your CV, and manages the application workflow — up to the point where Jobs.bg
requires a human, which it detects and hands back to you rather than working
around.

Everything runs on your machine. Your CV never leaves it.

---

## What it does

| Stage | Behaviour |
|---|---|
| **Discovery** | Paginates the Jobs.bg IT section for your city with a real browser, at a polite request rate. |
| **Normalization** | Cleans each listing, parses salary/dates/experience, and assigns a stable fingerprint. |
| **Classification** | Decides *is this IT?*, *is this location viable?* and *what is the minimum seniority?* — in Bulgarian and English. |
| **Matching** | Scores 0–100 across seniority, location, technology overlap, experience, language and employment type. Produces strengths, missing skills, disqualifiers and a recommendation. |
| **Decision** | `>= 90` APPLY · `75–89` REVIEW · `< 75` SKIP. All configurable. Defaults to review-only. |
| **Applications** | Selects the right CV, writes a cover letter, fills the Jobs.bg form, attaches the CV — then stops at any CAPTCHA or employer questionnaire. |
| **Dashboard** | Local web UI for triage, job detail, match analysis, applications and settings. |
| **Scheduling** | Optional daily scan. |

---

## Requirements

* Python 3.12+
* Linux, macOS or Windows with a desktop session (the browser runs **headed** by default — see [Why headed](#why-the-browser-runs-headed))
* No API key required. A cloud AI provider is optional.

---

## Install

```bash
# 1. dependencies (uv is recommended; https://astral.sh/uv)
uv sync --extra ai

# 2. the browser Playwright drives
uv run playwright install chromium

# 3. configuration (every value has a working default)
cp .env.example .env

# 4. first-run setup: creates the database, finds your CVs, builds your profile
uv run jobhunter init
```

`init` searches `~/Documents`, `~/Downloads` and `~/Desktop` for CV-looking
files, extracts the text locally, and pre-fills your profile from it. You can
also pass files explicitly:

```bash
uv run jobhunter init --cv ~/Documents/CV.pdf --cv ~/Documents/CV_BG.pdf
```

Check everything is wired up:

```bash
uv run jobhunter doctor
```

---

## Run

```bash
uv run jobhunter serve      # dashboard at http://127.0.0.1:8000
```

Then press **Run scan**, or from the terminal:

```bash
uv run jobhunter scan                  # one pass
uv run jobhunter scan --limit 40       # cap the listings processed
uv run jobhunter scan --entry-level    # use the site's own entry-level filter
uv run jobhunter jobs --min-score 75   # review results in the terminal
```

A Chromium window opens while a scan runs. That is intentional — leave it alone
and it will close itself.

### Applying

```bash
uv run jobhunter apply 42            # prepare: fill the form, stop before submitting
uv run jobhunter apply 42 --submit   # submit, if the form has no human-only gate
```

`--submit` is an explicit, per-job decision you are making. Unattended
submission during scheduled runs is separately gated by `AUTO_APPLY`.

### Scheduling

```bash
uv run jobhunter schedule    # foreground; daily at SCAN_AT_HOUR
```

---

## Configuration

All settings live in `.env` (see `.env.example` for the annotated list). The
ones that matter most:

| Variable | Default | Meaning |
|---|---|---|
| `SEARCH_LOCATION` | `Varna` | City to search. Resolved to the Jobs.bg location id. |
| `AUTO_APPLY` | `false` | Master safety switch for unattended submission. |
| `AUTO_APPLY_THRESHOLD` | `90` | Score at or above which a job is recommended APPLY. |
| `REVIEW_THRESHOLD` | `75` | Below this, a job is skipped. |
| `MAX_SENIORITY` | `mid` | Jobs whose entry bar is above this are disqualified. |
| `BROWSER_HEADLESS` | `false` | Leave false. See below. |
| `REQUEST_DELAY_SECONDS` | `3.0` | Delay between page loads. Do not lower. |
| `AI_PROVIDER` | `auto` | `auto`, `rule_based`, `anthropic` or `openai`. |
| `MAX_PAGES_PER_SCAN` | `5` | 20 listings per page. |

Matching, scanning and automation settings are also editable live in the
dashboard's **Settings** page, which overrides `.env`. API keys are only ever
read from `.env`, never from the UI.

### AI configuration

The system ships with a deterministic **rule-based** scorer that needs no
network and no key. It is the default and the fallback.

To use a cloud model instead, set one key in `.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
# or
OPENAI_API_KEY=sk-...
```

If a provider errors, times out or is misconfigured, scoring silently falls back
to the rule engine — a scan never fails because of an API problem.

---

## What is sent to an external AI service

Only when you configure a cloud provider. Per job, the request contains:

* **From your profile:** years of experience, target seniority, skills,
  frameworks, databases, tools, spoken languages, a coarse education *level*
  (e.g. `"bachelor"`), preferred locations, and your summary paragraph.
* **From the listing:** title, company, city, requirements and the description,
  truncated to `AI_MAX_DESCRIPTION_CHARS`.

Never sent: your name, email, phone, CV file, CV text, links, or the name of
your university. `SEND_CV_TEXT_TO_AI` is `false` and nothing reads it as true.

With `AI_PROVIDER=rule_based` (or no key set) **nothing leaves your machine**.

---

## Why the browser runs headed

Jobs.bg sits behind Cloudflare. During development, a headless browser reliably
received the managed-challenge interstitial ("Just a moment…" / "Един момент…"),
while an ordinary visible Chromium did not. Running headed is simply using a
normal browser; it is not an evasion technique, and no stealth patching,
fingerprint spoofing or challenge-solving is used anywhere in this project.

It also means that when a challenge *does* appear, the window is already open
in front of you and you can complete it yourself.

Relatedly, the first navigation of a run lands on the Jobs.bg home page before
opening a search URL. A browser profile with no cookies that requests a search
URL directly is answered with an empty 403; arriving via the front page, the way
a person would, avoids that. Without it, every first run was blocked.

You can set `BROWSER_HEADLESS=true`, but expect scans to be blocked.

---

## Authentication

The system never handles your Jobs.bg password.

A persistent browser profile lives in `data/browser-profile/`. Log into Jobs.bg
once in the window that opens during a scan and the session persists across
runs. When a login wall is hit, the run reports it and tells you to sign in.

Many listings also offer "Кандидатствай без акаунт" (apply without an account),
which the system prefers automatically when no session is present.

---

## Testing

```bash
make test        # 343 tests
make test-cov    # with coverage
make check       # lint + typecheck + tests
```

The suite never touches the network. Parser tests run against real Jobs.bg HTML
saved in `tests/fixtures/`, and the browser is replaced by fakes elsewhere.

---

## Troubleshooting

**"Could not launch Chromium"** — run `uv run playwright install chromium`. On a
bare Linux box you may also need system libraries; `playwright install-deps`
needs root, but most desktop distributions already have them.

**Scan reports BLOCKED** — Jobs.bg showed a challenge. Complete it in the open
browser window, then run the scan again. Do not lower `REQUEST_DELAY_SECONDS`;
if anything, raise it.

**Everything scores low** — check your profile. `uv run jobhunter profile show`.
An empty skills list means nothing can overlap. Note also that a listing whose
description was never fetched is deliberately capped below the auto-apply
threshold (see [Limitations](#known-limitations)).

**No jobs found** — confirm `SEARCH_LOCATION` is a city Jobs.bg knows. Unknown
names are ignored rather than guessed, which broadens the search to all of
Bulgaria.

**Dashboard shows stale settings** — settings changed in `.env` need a restart;
settings changed in the UI apply immediately.

---

## Known limitations

These are boundaries of the site or deliberate safety choices, not bugs.

1. **Submission usually needs a human.** The Jobs.bg application form's submit
   button is wired to reCAPTCHA. The system fills the form and stops. It will
   never attempt to solve a CAPTCHA.
2. **Employer questionnaires are never auto-answered.** Many listings attach
   free-text questions. Answering them automatically would mean inventing
   content on your behalf, so the form is prepared and left for you.
3. **External listings cannot be automated.** Roughly a third of IT postings
   hand off to a company ATS (Workday, UKG, …). These are recorded with their
   link and marked as needing a manual step.
4. **Only listings that were actually read can reach APPLY.** Detail pages cost
   a request each, so a bounded number are fetched per scan, highest-scoring
   first. Anything scored on its listing card alone is capped below the
   auto-apply threshold — the system will not recommend applying to a job it
   has not read.
5. **The site's own location filter is loose.** A search for Varna also returns
   Sofia-based remote roles; the local classifier re-checks every location.
6. **Bulgarian text is handled by keyword rules**, not a language model, unless
   you configure a cloud provider.

---

## Safety

The system deliberately does **not**: bypass CAPTCHA or bot detection, spoof
fingerprints, defeat access controls, scrape at high volume, or ignore
`robots.txt` (which Jobs.bg currently serves as fully permissive). Requests are
paced with a jittered delay, retries are bounded, and any anti-automation
challenge stops the affected workflow and records `BLOCKED`.

---

## Documentation

* [ARCHITECTURE.md](ARCHITECTURE.md) — design, module map, and how the Jobs.bg
  contract was derived
* [OPERATIONS.md](OPERATIONS.md) — day-to-day running, tuning and recovery

## Licence

Personal project; no licence granted.
