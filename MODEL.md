# Choosing the local model

The matcher runs on this machine. This file records what that machine is, which
models were measured on it, and why the default is what it is.

Every number here was produced by `jobhunter benchmark` against
`evaluation/dataset.json` — 41 real Jobs.bg listings labelled by hand. Nothing
here is estimated.

## The machine

| | |
|---|---|
| CPU | Intel Core i5-1135G7, 4 cores / 8 threads |
| RAM | 15 GiB |
| GPU | NVIDIA GeForce MX450, **1.6 GiB usable VRAM** (CUDA 7.5) |
| iGPU | Intel Iris Xe (dropped by Ollama by default) |
| Disk | 327 GiB free |
| Runtime | Ollama 0.33.3, unpacked into `~/.local/ollama` (no root) |

**The 1.6 GiB of VRAM is the binding constraint** and it decides almost
everything else. A 3B model quantised to Q4 needs about 1.9 GiB once its KV
cache is included, so it does not fit; Ollama splits it roughly 67/33 between
CPU and GPU and generation runs at about 6 tok/s. Nothing in the 1–4B class
runs *fast* here. The question is not which model is quickest but which is
accurate enough to be worth two minutes of waiting.

Two settings follow directly from that limit:

* `LOCAL_MODEL_NUM_CTX=6144` — the KV cache competes with the weights for the
  same 1.6 GiB, so context is not free: 8192 pushed the resident size to 2.4 GiB
  and cut GPU offload to 25%, while 4096 raised it to 33%. But 4096 does not fit
  the longest real prompt, and the resulting truncation is silent, so 6144 is the
  smallest setting that is actually correct here.
* `LOCAL_MODEL_NUM_PREDICT=900` — enough for the full schema. At 700 the JSON
  was being truncated mid-string.

## Failure modes found while measuring

These cost more accuracy than the choice of model, and are fixed in code rather
than by swapping weights.

**Reasoning models must have thinking disabled.** `qwen3:1.7b` left to think
spent its entire token budget on 3,121 characters of reasoning and emitted *zero
characters of JSON*. The client now sends `think: false` and retries without it
for models that reject the option.

**The schema must be flat.** Ollama compiles the JSON Schema to a GBNF grammar,
and that compiler rejects `$ref`, `anyOf` and `maxLength` — all of which
Pydantic's `model_json_schema()` emits. The schema in `ai/local_model.py` is
written out by hand for this reason.

**Field order in the schema is load-bearing.** The grammar emits required
properties in the order given, so the evidence fields are listed before
`decision`. The model settles the facts before it commits to an answer instead
of justifying an answer after the fact.

**Arrays need `maxItems`.** They are what make the response long, and an
unbounded list runs past `num_predict` and truncates the JSON.

**A prompt that does not fit is truncated silently.** This is the failure that
cost the most and was hardest to see: the model returns confident, well-formed
JSON whether or not it still has the instructions or the requirements section.
Measured over the labelled set the prompt reaches ~4,063 tokens, which does not
fit a 4096 context alongside a 900-token reply. The context is now 6144, every
response's `prompt_eval_count` is checked against it, and a test renders the
real prompt for all 41 cases and asserts it still fits.

**Validation must be lenient about numbers and strict about meaning.** A
measured qwen3 response returned `"confidence": 3`. Rejecting the whole
evaluation over one bad number threw away a sound assessment, so an
uninterpretable confidence now falls back to 0.5 — never upward to 1.0, which
would assert certainty the model never expressed.

## How the models were compared

Four models in the 1-4B class were run over a stratified 14-case subset
(3 APPLY / 3 REVIEW / 8 SKIP) drawn from the labelled set, all on prompt v3 and
identical settings, before the best was run over all 41 cases.

| model | on disk | resident | GPU offload | seconds/job |
|---|---|---|---|---|
| qwen3:1.7b | 1.4 GB | 1.9 GB | 33% | 109 |
| **qwen2.5:3b** | 1.9 GB | 2.3 GB | 26% | **99** |
| llama3.2:3b | 2.0 GB | 2.4 GB | ~25% | 193 |
| gemma3:4b | 3.3 GB | 3.8 GB | **3%** | 180 |

Multilingual capability was the reason for the shortlist: roughly half of the
listings are in Bulgarian and many mix Bulgarian prose with English technology
names, which rules out models that only handle English well.

Note what the VRAM ceiling does to the largest one. gemma3:4b needs 3.8 GB
against 1.6 GB available, so Ollama runs it 97% on the CPU — it is not really
using the GPU at all. At 180 s/job a 94-listing scan would take about four and
a half hours, which is not a daily tool.

## Screening results

Fourteen stratified cases (3 APPLY / 3 REVIEW / 8 SKIP), all models on prompt
v3-v5, identical settings. The two stub baselines are included because most
listings genuinely are skips, and both headline metrics can be gamed by refusing
to decide in one direction or the other.

| matcher | acc | worth-surfacing recall | precision | harmful | seniority | location | skips issued |
|---|---|---|---|---|---|---|---|
| always-skip stub | 57% | 0% | 0% | 3 | – | – | 14 |
| always-review stub | 21% | 100% | 43% | 0 | – | – | 0 |
| legacy rule score | 57% | 33% | 67% | 2 | 79% | 79% | 12 |
| qwen3:1.7b | 14% | 100% | 43% | **6** | 22% | 11% | 0 |
| qwen2.5:3b | 21% | 83% | 42% | 2 | 50% | 57% | 2 |
| llama3.2:3b | 14% | 100% | 43% | 0 | 80% | 11% | **0** |
| gemma3:4b | 14% | 100% | 43% | 1 | 75% | 58% | **0** |

Read the last column first. Against **8 labelled skips**, three of the four
models issued **none or almost none**. That is why they share identical headline
numbers with the always-review stub: they had stopped discriminating.

The cause was in the prompt, not the models. Up to v6 it said *"when genuinely
torn, choose review"*, and every model obliged. Prompt v7 states the base rate —
most listings are skips, and saying so is the useful answer — lists concrete
skip triggers, and defines "review" as *"I could not decide"* rather than *"I
would rather not say"*.

The same model and the same 14 cases, before and after that one change:

| qwen2.5:3b | v6 | **v7** |
|---|---|---|
| decision accuracy | 21% | **57%** |
| surfaced precision | 42% | **67%** |
| APPLY recall | 0% | **33%** |
| harmful errors | 2 | **1** |
| predictions | 10 review / 2 skip / 2 apply | **8 skip / 4 review / 2 apply** |
| *(labels)* | | *8 skip / 3 review / 3 apply* |

## The selected model

**qwen2.5:3b, Q4, via Ollama.** Roughly 99 seconds per listing on this machine,
2.3 GB resident, about a quarter of it on the GPU.

It was chosen because it is the only candidate that both discriminates and runs
at a usable speed:

* **qwen3:1.7b** — six harmful errors out of fourteen, recommending APPLY for a
  Senior Full-Stack WordPress role and a Sofia-based job. Being smaller bought
  nothing: at this VRAM it is no faster.
* **llama3.2:3b** — the slowest at 193 s/job, worst location accuracy (11%), and
  statistically indistinguishable from the always-review stub.
* **gemma3:4b** — the best supporting claims of the four (75% seniority, 58%
  location) and its reasoning reads well in the logs, but it cannot fit the GPU
  at all and costs 180 s/job. Worth revisiting on a machine with more VRAM.

## Results

<!--BENCHMARK-->

## Known weaknesses

<!--WEAKNESSES-->
