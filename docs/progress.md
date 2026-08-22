# The Loom — progress

**Goal.** A story on a projector that the whole room writes at once. Anyone scans a QR code and throws
in a phrase; it is pinned into the diffusion canvas as frozen tokens and the story visibly re-denoises
around it. Nothing restarts, nothing breaks — it absorbs. Visuals via Nano Banana on each commit.

Only a diffusion LM can do this: an autoregressive model handed a mid-story insertion must either
regenerate everything after it or patch forward-only, and in neither case can the text *before* the
insertion react to it.

*Session of 2026-08-22.*

---

## Done

### Feasibility established

The existing backend already had most of what The Loom needs — per-position canvas frames over SSE, a
UI that colours positions by stability, and non-causal attention so ripples genuinely travel both
directions within a canvas. Latency measured at **3–10 s per reweave round** (see
[backend.md](backend.md#measured-latency)), scaling gently with story length. `MAXTOK` auto-sizes to
32768 on this box, so a story can grow to ~25k tokens of context.

### Pin patch — shipped and verified

The one missing capability. `pins` freezes chosen canvas positions for the whole denoise; the rest of
the canvas reweaves around them. Contract in [backend.md](backend.md#pinning).

| file | change |
|---|---|
| `examples/diffusion/diffusion.h` | `diffusion_eb_params::pinned` — canvas-length token array, `LLAMA_TOKEN_NULL` = free |
| `examples/diffusion/diffusion.cpp` | pins seed the canvas, survive every renoise, are re-asserted after each step's sampling with entropy 0 so they sort first and consume none of the MI budget; convergence now averages entropy over **free positions only** |
| `examples/diffusion-gemma-server/diffusion-gemma-visual-server.cpp` | `"pins"` parsing, server-side tokenization, per-pin `special` opt-in, new `P <block> <json>` record reporting where each span landed |
| `dg_openai_server.py` | validates and forwards `pins` on both endpoints; new `{"type":"pins"}` SSE event |
| `frontend/README.md` | documented |

Originals in `.pinpatch-backup/`. All four diffusion targets build clean, no warnings, ~7 s
incremental (C++ only, no CUDA recompile).

Verified:

- Unpinned runs at the same seed are byte-identical to each other **and provably identical to
  pre-patch** — the random token is still drawn for every position so the RNG stream is unchanged, and
  `n_free == C` when nothing is pinned.
- Pins land verbatim at requested positions; multiple contributors' pins coexist.
- **Backward ripple confirmed**: with a pin at position 200, prose at position ~10 changed vs control.
- Submissions tokenize with `parse_special=false` — they cannot inject `<|channel>` or an eog token.

### The thought channel — investigated, not fixable from the prompt

The model spends its canvas writing a planning outline. **There is no think token in the prompt to
remove**: the chat template's generation prompt is a bare `<|turn>model\n`, and `<|channel>thought`
appears in the template only when replaying a prior assistant message that had *both*
`reasoning_content` and `tool_calls`. The model opens the channel on its own, from training.

Eight suppression attempts, all measured, all failed: empty channel pinned at position 0; bare
`<channel|>` close marker; a one-line thought; a prose prime at position 0; no system prompt at all;
an explicit anti-outline system prompt; a one-shot prose example; and combinations. Several runs
produced the outline as **plain text with no channel markers at all**, so banning the token at the
decoder would remove the marker and not the behaviour.

What it costs on an empty canvas, at `max_tokens: 768`:

| block | content | cumulative |
|---|---|---|
| 0 | planning outline | 6.7 s |
| 1 | first draft of the prose | 14.6 s |
| 2 | self-check, `<channel|>`, **final prose** | 17.4 s |

~2.6× paid for planning and a draft that gets thrown away.

### Gap-filling — the fix, and the architecture

Pinning the *existing prose* into the canvas with gaps leaves no room for an outline. Measured:
**5.9 s, one block, zero outline markers**, all pinned spans verbatim — and the model absorbed the
audience phrase by rewriting its neighbours into a simile:

> the light flared to life, a golden eye cutting through the gloom, but it felt like **a stolen accordion**

This changed the architecture for the better. The story does **not** have to live in the prompt as
settled history — it can live *in the canvas*, pinned. So the reweave window is not stuck at the end
of the story: pin any 256 tokens, reweave them, commit, slide. A phrase dropped into paragraph 1 of 6
becomes the same 6-second operation repeated as the window slides down the screen, which makes "the
opening retroactively plants a setup" literally true rather than a stretch.

---

### Weave planner — built, and the seam policy is settled

`loom/weave.py` turns "the story so far" plus a queue of audience phrases into a `pins` array. It splits
the paragraph into sentences, decides which stay pinned and which dissolve into gaps, sizes the gaps,
places the submissions, and fits everything to the 256-position budget. `plan_weave()` is pure layout —
it never talks to the model — so a plan can be printed and inspected before anything is generated.

Pin positions are absolute, so the planner needs exact token counts. It gets them from the backend's own
vocab through a new `POST /v1/tokenize` (see [backend.md](backend.md)), rather than carrying a second
tokenizer that could disagree with the model.

`loom/seamtest.py` scores policies against the live model on the defects that actually turned up in
hand-run output: `outline`, `lc_after_dot`, `fused`, `bracketed`, `meta`, `punct_run`, `dup_word`,
`lost_pins`. `--stability N` re-checks the default across N seeds.

**What the experiment settled.** The starting hypothesis was wrong, and the measurements say so:

| policy | verdict |
|---|---|
| withhold the terminal `.` on **every** sentence | **rejected** — scored worst (3 outlines, 2 fusions). Without a pinned period the model often fails to supply one and words fuse: `"dry kindlingThe suitcase"`. A lowercase word after a period is the milder defect |
| withhold it on the **final kept sentence only** (`open_last`) | **adopted** — that is the growth point, and the model wants to extend that sentence. Fixed the one seam that survived every other policy: `"until they gave. with a metallic click."` → `"until they gave way with a sharp click."` |
| cap the free tail (`max_tail=24`) | **adopted** — 150 idle positions after the weave is where the model starts writing planning notes and meta-commentary about the prompt. Surplus goes into the working gaps instead |
| rewrite both neighbours of a submission | **rejected** — reverted to outlining |
| "keep every word already written" in the prompt | **rejected** — made the model treat pins as quoted material and wrap them in brackets. The pins already guarantee preservation; saying so only gives it something to react to. Keep the prompt minimal |

**Result: the default policy scores zero on every metric, on 4 of 4 seeds, at 5.0–6.0 s per round.**

> Behind him, the horizon seemed to lean in, a silent, grey witness. He set it on the scarred table and
> worked the latches until they gave way with a sharp metallic click.

One caveat worth designing around: a round carrying **three** submissions at once scores 1 rather than 0
and shows the model straining to make every pin grammatical (`"it was Tuesday on Mars Tuesday that the
fog began..."`). Quality is best at one or two phrases per round. A round is only ~6 s, so the Loom
server should cap submissions per round and let the rest wait — which also puts the queue on the
projector, where it reads as theatre rather than as a limitation.

### Loom server — built and driven end to end

`loom/server.py` owns the story and drives the rounds; it talks to the model over HTTP and relays the
denoising frames to the projector. Contract and component notes live in **[`loom/docs/`](../loom/docs/)**
— [`api.md`](../loom/docs/api.md) for the HTTP surface, [`progress.md`](../loom/docs/progress.md) for
the design decisions and what each one cost to learn.

Verified with three phrases from two contributors over six rounds: all three present in the final story
and still present after later rounds rewrote around them, two coherent paragraphs, contributor colours
stable, revision log complete, 91 frames relayed, ~6 s per round.

Four bugs only a full drive could find, each now a rule:

- **Phrases were being deleted.** A later round picked the sentence holding someone's phrase as its
  rewrite target and it vanished. Everything the room has landed is now protected from dissolution —
  the one failure the exhibit cannot afford, since the contributor has walked away and cannot resend.
- **The story ran away** — five paragraphs in four rounds. A small live paragraph means enormous gaps,
  which the model fills with 150 tokens of new prose, which splits again. The live paragraph is now
  trimmed *back to* near canvas size, so a round rewrites it instead of extending it.
- **Idle rounds stalled**, reproducing the paragraph verbatim. They now get one sentence to rewrite,
  chosen from the round number so the log still replays exactly.
- **The model copies itself** given a wide gap, restating a sentence it can already see. Detected and
  rejected: a round is thrown away, and its phrases requeued, if fewer than 75% of pinned spans survive
  or a 12-word run is newly duplicated. The story is never left worse than it was found.

### A crash in the model backend, found and fixed

Driving the Loom aborted `llama-diffusion-gemma-visual-server` outright:

    [json.exception.type_error.316] invalid UTF-8 byte at index 0: 0xA0

A token's piece is often only *part* of a multi-byte UTF-8 character — byte-fallback tokens are single
bytes, and the decoder random-initialises all 256 canvas positions, so partial sequences are constant.
`json::dump()` throws on those and the frame callback runs outside any `try`, so one unlucky position
killed the process mid-generation. Latent all along; the Loom streams every frame of every round and so
hit it far harder than anything before. All model-derived dumps now substitute U+FFFD instead of
throwing.

**This one matters beyond the Loom** — it could have taken the backend down mid-demo at any point.

## In progress

Nothing. The next item below is unstarted.

---

## Next

1. **Projector UI** — fork `frontend/index.html`; it already colours each position by `held`, so
   contributor colours key off the `id` in the new `pins` event.
2. **Phone submit page + Tailscale Funnel.** Deferred. Everything is Tailscale-bound by policy and
   judges' phones are not on the tailnet; `tailscale funnel 8081` is the fix. **Confirm Funnel is
   permitted in the tailnet ACL well ahead of the event** — it is an admin-console toggle and the only
   blocker with a human in the loop.
3. **Nano Banana on commit.** Deferred. The box reaches `generativelanguage.googleapis.com` (403, no
   key); `GEMINI_API_KEY` is unset. Cloud, ~2–8 s/image — *not* the 20–60 FPS StreamDiffusion path, and
   the better choice anyway: one striking plate per beat, edited from the previous plate rather than
   regenerated. Must run concurrently with the next denoise round and never block the text path; cache
   every plate and keep showing the last one on failure.

## Known limits

- **256 positions is one paragraph** (~190 words). Pinned story text eats most of them, so the live
  reweaving region is one paragraph at a time.
- **A committed block becomes prompt.** Within a block, ripples are genuinely bidirectional; across
  blocks they are a forward cascade of re-denoised paragraphs. Still nothing an autoregressive model
  can do, but say it precisely in the pitch rather than overclaiming.
- **One request at a time.** Queue depth is worth putting on the projector — "7 phrases waiting to be
  woven" reads as theatre, not as a limitation.
