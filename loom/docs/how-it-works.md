# How the backend works (and what to say about it on stage)

Two services, two very different jobs. Keep this distinction in your head and in your pitch:

```
  phones ──POST /submit──▶  ┌─────────────┐        ┌──────────────────────────┐
                              │  Loom        │──HTTP──▶│ DiffusionGemma shim      │
  projector ◀──SSE /stream── │  server.py   │◀────────│ dg_openai_server.py      │
                              │  weave.py    │         │ (wraps a diffusion LLM)  │
                              └─────────────┘         └──────────────────────────┘
                              "the writer's room"       "the model"
```

- **The Loom** (`loom/server.py`, `loom/weave.py`) owns the *story*: the queue of audience phrases, the
  paragraphs, the revision history, the SSE feed to the projector. It contains zero model logic.
- **The DiffusionGemma shim** (`dg_openai_server.py`, one level up) owns the *model*: a diffusion
  language model (`diffusiongemma-26B-A4B-it`) running on-device (Jetson Thor), spoken to over an
  OpenAI-ish HTTP API plus one bespoke endpoint for streaming the raw denoising canvas.

Full contracts: [`api.md`](api.md) (Loom) and [`../../docs/backend.md`](../../docs/backend.md) (model shim).

---

## 1. The core trick: this is a diffusion model, not an autoregressive one

Everything interesting about this project follows from one fact: the model doesn't generate text
left-to-right like GPT. It starts with a **fixed-size canvas of 256 token positions**, all noisy, and
repeatedly denoises the *whole block at once* until it converges — more like Stable Diffusion for
pixels, but for tokens. That's the "diffusion" in DiffusionGemma.

This has two consequences the whole project is built around:

1. **You can pin positions.** Because generation isn't sequential, you can freeze arbitrary positions in
   the canvas to caller-supplied text and the model denoises *around* them for the entire process —
   never overwriting them, and reacting to them non-causally (text *before* a pin can be influenced by
   it, which an autoregressive model literally cannot do).
2. **An empty canvas is a trap.** Asked to write into a blank 256-token canvas, the model reliably
   produces a planning outline instead of prose — this was measured against ~8 different prompting
   strategies, all of which failed (see `../../docs/progress.md`). No prompt fixes it. The only thing
   that works is leaving the model no room to outline: **pin the story's existing text back into the
   canvas with small gaps, and it can only write connective tissue in the gaps.**

That second point is the whole product idea: **the "Loom" *is* the pinning mechanism.** The story never
restarts because it's never generated fresh — it's always the previous paragraph, re-pinned, with the
audience's new phrases dropped into a few gaps.

---

## 2. The round loop, in one pass

Every ~6 seconds, one "round" happens:

1. **Batch.** Pull up to 2 pending phrases off the queue that target the same paragraph (a canvas is one
   paragraph — you can't mix two paragraphs in one 256-token block).
2. **Plan (`weave.py`).** Split the paragraph into sentences. Decide which sentences stay pinned verbatim
   and which get dissolved into a gap (a submission's neighbouring sentence has to move to make room for
   it). Ask the model's own tokenizer (`/v1/tokenize`) for exact token counts, lay everything into 256
   positions, size the gaps (min 4, max 60 tokens — bigger gaps make the model loop: *"a grinding
   grinding grinding against the shore"*).
3. **Generate.** POST the plan as `pins` to `/v1/diffusion/stream`. Stream every denoising frame straight
   through to the projector via SSE.
4. **Accept or reject.** Check that ≥75% of the pinned story spans survived and nothing was newly copied
   from elsewhere. If not, throw the round away — the story is left exactly as it was, and the phrases go
   back on the queue. A rejection costs 6 seconds and nothing else.
5. **Commit.** Update the paragraph, log the revision (full before/after/seed, replayable), queue a
   "ripple" so every later paragraph re-weaves to stay consistent with the change.
6. **Split.** If the live paragraph has grown past its budget, trim it back and let the overflow settle
   into the paragraph behind it.

With nobody submitting, an idle round still fires every 25s and is handed one sentence to rewrite —
otherwise the projector visibly freezes, which reads as broken rather than as calm.

---

## 3. Talking points for the presentation

**The hook:** *"This is a diffusion language model — the same kind of process that makes Stable Diffusion
images, but for text. Instead of writing one word at a time, it denoises 256 tokens at once. That means
we can freeze part of the canvas and watch the model write only around it — live, on a big screen, with
the whole room able to see their own words never disappear."*

**What's actually novel here (not just "a demo of an existing model"):**
- The **pinning technique itself was co-designed with the model's serving stack** — there's a patch to
  llama.cpp (`patches/llama.cpp-diffusion-pins.patch`) adding pin support to the diffusion sampler. This
  isn't a prompting trick, it's a capability that didn't exist in the inference engine before this project.
- **Solving the outline failure mode.** A blank-canvas diffusion LLM writing planning notes instead of
  prose is a real, measured limitation (`docs/progress.md` documents ~8 failed prompt strategies). Pinning
  the existing prose back in is the fix — and it's also why the story can run forever without restarting.
- **Non-causal attention as a feature, not a footnote.** Text before a pin reacts to the pin. That's a
  structurally different capability from GPT-style generation, and it's the reason "weave around an
  audience phrase" even works.
- **The system is measured, not vibes-based.** Every policy constant in `weave.py` (gap sizes, punctuation
  handling, when to leave a sentence open-ended) exists because of a specific observed failure and was
  re-validated with `seamtest.py` — "zero defects across 8 metrics and 4 seeds" is a real, reproducible
  result, not a one-off cherry-picked run.
- **It's honest about failure.** ~1/3 of *idle* rounds get rejected and thrown away automatically — the
  system self-polices rather than ever showing the audience broken or duplicated prose. That's worth
  saying out loud; it demonstrates the guardrails work, not that the model is unreliable.

**Numbers worth having on a slide:**
| | |
|---|---|
| canvas size | 256 tokens per diffusion block (fixed by the model) |
| round latency | ~6s for a single-block reweave, on a Jetson Thor, Q8_0 quantization |
| model | `diffusiongemma-26B-A4B-it`, resident in-process via `llama-diffusion-gemma-visual-server` |
| concurrency | the model is single-threaded — every request serializes through one lock, which is *why* the Loom queues and batches instead of firing one generation per phone |
| batch size | 1–2 audience phrases woven per round (3+ measurably degrades prose quality) |
| acceptance bar | a round is discarded unless ≥75% of the existing story survives verbatim and nothing was newly plagiarized |

**Likely audience questions and honest answers:**
- *"What happens if two people submit at once?"* → They queue. The model can only do one generation at a
  time, so the "queue depth" counter is shown as part of the show, not hidden as a limitation.
- *"Does it ever lose someone's words?"* → No — every phrase ever accepted is tracked in a `protect` set
  and the planner is forbidden from ever dissolving the sentence holding it, for the rest of the story's
  life.
- *"Is this persisted / can it crash?"* → Currently in-memory only; a restart loses the story. Known and
  documented (`progress.md` "Known limits") — worth pre-empting rather than getting caught by it live.
- *"Why not just use GPT-4/Claude for this?"* → Because the whole mechanic — freezing part of the canvas
  while an audience phrase gets woven around it live on screen, with earlier text visibly reacting to a
  phrase submitted afterward — is only possible with a non-causal diffusion model. An autoregressive model
  would have to regenerate the tail from scratch and could not react backwards.

---

## 4. Where each piece of the story lives in the code

| concern | file |
|---|---|
| model process, HTTP/SSE shim, pinning support | `../../dg_openai_server.py` (+ llama.cpp patch) |
| canvas layout / pin placement algorithm | `weave.py` → `plan_weave()` |
| policy constants (gap sizes, punctuation rules) and *why* each exists | `weave.py` `Policy` class docstrings |
| round loop, queueing, accept/reject, splitting | `server.py` → `Loom._run_round()` |
| HTTP/SSE surface for phones + projector | `server.py` `Handler` class, contract in `api.md` |
| measured evidence for every policy default | `seamtest.py` |
