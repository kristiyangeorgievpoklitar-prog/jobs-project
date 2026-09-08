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

* `LOCAL_MODEL_NUM_CTX=4096` — at 8192 the KV cache alone pushed the resident
  size from 1.9 GiB to 2.4 GiB and cut GPU offload from 33% to 25%. The prompt
  is ~1.8k tokens and the reply ~900, so 4096 is sufficient.
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

**Validation must be lenient about numbers and strict about meaning.** A
measured qwen3 response returned `"confidence": 3`. Rejecting the whole
evaluation over one bad number threw away a sound assessment, so an
uninterpretable confidence now falls back to 0.5 — never upward to 1.0, which
would assert certainty the model never expressed.

## Results

<!--BENCHMARK-->

## Known weaknesses

<!--WEAKNESSES-->
