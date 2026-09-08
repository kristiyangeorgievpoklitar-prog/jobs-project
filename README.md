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
| **Description recovery** | Reads the posting body out of the sandboxed iframe Jobs.bg renders it in. Without this the only "description" available is navigation chrome. |
| **Stage 1 filter** | Removes what no reading could rescue — unambiguously senior titles, non-software roles, listings you already skipped. Never judges skill overlap. |
| **Stage 2 local model** | A small instruct model reads the full posting and your CV, separates mandatory from nice-to-have requirements, infers the real seniority, decides the location from the text, and judges each requirement against evidence in your profile. |
| **Policy** | Refuses to endorse what the evidence does not support: a failed model call, a posting whose text was never fetched, a missing mandatory requirement, a job in another city. Only ever downgrades. |
| **Decision** | **APPLY / REVIEW / SKIP**, with the reasoning and the specific requirements behind it. There is no headline score. |
| **Personalization** | Learns from what you actually decide, and reorders what you are shown. It never changes a decision. |
| **Applications** | Selects the right CV, writes a cover letter, fills the Jobs.bg form, attaches the CV — then stops at any CAPTCHA or employer questionnaire. |
| **Dashboard** | Local web UI that opens on "what should I apply to today?" |
| **Scheduling** | Optional daily scan. |

---

## The local model

The matcher is a small instruct model running on your own machine through
[Ollama](https://ollama.com). This is the default, not an option: the prompt
contains your CV, and a job hunt runs every day for months.

```bash
# install Ollama (no root needed - unpack into your home directory)
curl -fL https://github.com/ollama/ollama/releases/latest/download/ollama-linux-amd64.tar.zst \
  -o /tmp/ollama.tar.zst
mkdir -p ~/.local/ollama && tar --use-compress-program=unzstd -xf /tmp/ollama.tar.zst -C ~/.local/ollama

# start the server and fetch the model
~/.local/ollama/bin/ollama serve &
~/.local/ollama/bin/ollama pull qwen2.5:3b

# check the agent can see it
uv run jobhunter doctor
```

Configure which model is used in `.env`:

```ini
AI_PROVIDER=local
LOCAL_MODEL=qwen2.5:3b
LOCAL_MODEL_HOST=http://127.0.0.1:11434
LOCAL_MODEL_NUM_CTX=6144
```

See [MODEL.md](MODEL.md) for how the model was chosen, what it costs per job on
this hardware, and where it is weak.

---

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
uv run jobhunter scan                  # discover, filter and evaluate
uv run jobhunter today                 # what is worth applying to right now
uv run jobhunter scan --limit 40       # cap the listings processed
uv run jobhunter scan --entry-level    # use the site's own entry-level filter
```

A Chromium window opens while a scan runs. That is intentional — leave it alone
and it will close itself.

### Evaluating and giving feedback

```bash
uv run jobhunter evaluate               # run the matcher over jobs not yet judged
uv run jobhunter evaluate --job-id 42   # re-run one job
uv run jobhunter evaluate --force       # ignore cached evaluations

uv run jobhunter feedback 42 apply                          # you applied
uv run jobhunter feedback 42 skip --reason too_senior       # and why
uv run jobhunter preferences                                # what it has learned
```

Feedback is the only ground truth the system gets. Reasons are one of
`too_senior`, `wrong_location`, `salary`, `technology`, `company`,
`not_interested`, `other`.

### Measuring it

```bash
uv run jobhunter dataset                # rebuild the labelled benchmark set
uv run jobhunter benchmark              # old scorer vs local model, on real listings
uv run jobhunter benchmark --models qwen2.5:3b,qwen3:1.7b
```

The benchmark reports **worth-surfacing recall** — of the jobs a human said were
worth a look, how many the matcher actually showed — alongside a deliberately
included `always_skip_baseline`, because most listings genuinely are skips and
plain accuracy flatters any cautious matcher.

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
   a request each, so a bounded number are fetched per scan. Anything judged on
   its listing card alone is downgraded to REVIEW by policy — the system will
   not recommend applying to a job it has not read.
5. **The site's own location filter is loose.** A search for Varna also returns
   Sofia-based roles; the location is re-derived from the posting text.
6. **A scan is slow the first time.** The local model takes about 100 seconds
   per listing on a laptop GPU, so a first pass over ~90 listings runs for well
   over an hour. The Stage 1 filter removes about 40% before the model sees them and
   evaluations are cached, so later scans are minutes. Run it overnight.
7. **A small model is a small model.** Measured on the labelled set, it hedges:
   it surfaces more jobs than it should rather than fewer, and its seniority and
   location calls are its weakest. It also occasionally names a technology you
   do not have. [MODEL.md](MODEL.md) has the numbers, and every recommendation
   shows the requirements it rests on so you can check it in seconds.
8. **Personalisation needs data.** Preferences need at least three observations
   of a value before they affect ranking, so the first week or two of use
   changes nothing. That is deliberate — three skips is not a pattern.

---

## Safety

The system deliberately does **not**: bypass CAPTCHA or bot detection, spoof
fingerprints, defeat access controls, scrape at high volume, or ignore
`robots.txt` (which Jobs.bg currently serves as fully permissive). Requests are
paced with a jittered delay, retries are bounded, and any anti-automation
challenge stops the affected workflow and records `BLOCKED`.

---

## Documentation

* [ARCHITECTURE.md](ARCHITECTURE.md) — design, module map, how the Jobs.bg
  contract was derived, and why the match score was removed
* [OPERATIONS.md](OPERATIONS.md) — day-to-day running, tuning and recovery
* [MODEL.md](MODEL.md) — the machine, the models measured on it, and why the
  default is what it is
* `evaluation/labels.json` — the hand-written labels the benchmark rests on,
  each with the reason it was labelled that way

## Licence

Personal project; no licence granted.
