# The Loom — component progress

Scope: `loom/`. For the model backend see `../../docs/backend.md`; for the project as a whole see
`../../docs/progress.md`.

    weave.py     pure layout — story + phrases -> a `pins` array. Never talks to the model.
    seamtest.py  scores weave policies against the live model. `--stability N` re-checks across seeds.
    server.py    owns the story, drives the rounds, relays frames to the projector.
    docs/api.md  the HTTP contract

*Session of 2026-08-22.*

---

## Done

### `weave.py` — the planner

Splits the paragraph into sentences, decides which stay pinned and which dissolve into gaps, sizes the
gaps, places submissions, and fits it all to 256 positions. Pin positions are absolute, so it needs
exact token counts and gets them from the model's own vocab via `POST /v1/tokenize` rather than carrying
a second tokenizer that could disagree.

Every policy default exists because of a defect seen in real output, and every one was measured against
its alternative. Reproduce with `python3 loom/seamtest.py`.

| default | why |
|---|---|
| `pin_terminal_punct=True` | Withholding the period everywhere was the starting hypothesis and it scored **worst** — the model often fails to supply one and words fuse (`"dry kindlingThe suitcase"`). A lowercase word after a period is the milder defect |
| `open_last=True` | ...except on the final kept sentence, which is the growth point. The model wants to extend that sentence, and pinning its period leaves `"until they gave. with a metallic click."` |
| `max_gap=60` | A gap of 100+ positions is not an invitation to write well, it is where the model loops: `"a low, guttural sound sound... a grinding grinding grinding"` |
| `max_tail=24` | 150 idle positions after the weave is where the model starts writing planning notes and commentary about the prompt |
| `dissolve_before=False` | Rewriting both neighbours of a submission reverted the model to outlining |
| minimal prompt | "Keep every word already written" made it treat pins as quoted material and wrap them in brackets. The pins already guarantee preservation |

Two later rules came out of running the full server rather than the bench:

- **`_open_seams`** — whitespace at every pin/gap boundary goes on the side the tokenizer wants it.
  Gemma carries a word's leading space inside the word token (`" It"` is one token, `"It"` is a rarer
  one), which makes both edges of a gap fragile in opposite directions. A pin ending `". "` spends a
  standalone space token and the model then *drops the next word* rather than use the spaceless variant
  (`"of listening.  wasn't just the crashing"`); a pin starting `"Behind"` forces the model to close its
  gap on a trailing-space token and it fuses instead (`"floorboards.Behind him"`). Strip the one, pad
  the other.
- **`_reclose`** — `open_last` is withdrawn when more pinned text follows across a wide gap. Over a
  short hop the model carries the clause over, which is the point; across eighty positions it starts a
  fresh sentence and never supplies the missing period. When the withheld sentence is the *last* pinned
  thing, the space after it is growth space and it is left open regardless of size.

**Result: zero defects on all eight scored metrics, across every policy and 4 of 4 seeds, at 4.1–6.0 s
per round** — including the three-submissions-at-once round, which scored 1 before the seam rules.

### `server.py` — the round loop

Story as a list of paragraphs with one live index; a submission queue drained in capped batches; a
revision log; SSE fan-out. Contract in [api.md](api.md).

Four bugs were found by driving it end to end rather than by reading it, and each one is a rule now:

- **Phrases were being deleted.** Round 2 picked the sentence holding `"a stolen accordion"` as its
  rewrite target and the phrase vanished from the story. Everything the room has ever landed is now
  passed to `plan_weave(protect=...)`, and a sentence containing a protected phrase is never dissolved.
  This is the one failure the exhibit cannot afford — the contributor has walked away and cannot resend.
- **The story ran away.** Splitting at 150 tokens and keeping two sentences left a tiny live paragraph,
  which meant enormous gaps, which the model filled with 150 tokens of new prose, which split again:
  five paragraphs in four rounds and never settling. The live paragraph is now trimmed *back to* 185
  tokens and overflow joins the paragraph behind it until that one is full. A nearly-full canvas
  rewrites the paragraph instead of extending it.
- **Idle rounds stalled.** With nothing to change, a round reproduced the paragraph verbatim and the
  projector froze. An idle round is now given one sentence to rewrite, chosen from the round number so
  the log still replays exactly.
- **A failed round ate the queue.** Phrases are taken off the queue before generating, so a backend
  failure lost them silently. They are put back at the head now.
- **The copy detector had to count, not test membership.** The first version skipped any run already
  present before the round, to avoid a livelock — but that let the real case through, where a sentence
  the paragraph already held *once* came back *twice*. The run was not new; the repetition was. Both
  tests are now measured against `before`, the repeat one by comparing counts.

### Verified end to end

A drive with three phrases from two contributors, over six rounds:

- all three phrases present in the final story, and still present after the rounds that rewrote around
  them — the whole point of `protect`
- two coherent paragraphs, no duplicated passages, no outline fragments, no fused seams
- contributor colours stable, revision log complete, 91 canvas frames relayed to the projector
- ~6 s per round

Two of six rounds were **rejected and the story left untouched** — one for a newly duplicated passage,
one because the model wandered and none of the five pinned spans survived. Both were idle rounds, so
nothing was lost. A rejection rate around a third on idle rounds is higher than it should be and worth
watching; it costs six seconds and protects the story, but if it holds at that level under load the
gap sizing on idle rounds is the thing to look at first.

### A crash in the model backend

Driving the Loom killed `llama-diffusion-gemma-visual-server` outright:

    terminate called after throwing an instance of 'nlohmann::json::detail::type_error'
      what():  [json.exception.type_error.316] invalid UTF-8 byte at index 0: 0xA0

A single token's piece is often only *part* of a multi-byte UTF-8 character — byte-fallback tokens are
individual bytes, and the entropy-bound decoder random-initialises all 256 canvas positions, so partial
sequences turn up constantly. `json::dump()` throws on those, and the frame callback runs outside any
`try`. One unlucky position aborts the process mid-generation.

Latent all along; the Loom just streams every frame of every round and so hits it far harder than
anything before. Fixed at source in `diffusion-gemma-visual-server.cpp`: all model-derived dumps now go
through `dump_lossy()`, which substitutes U+FFFD instead of throwing. One replacement character in one
cell for one step is invisible, and the `C` records remain authoritative.

---

## Known limits

- **Nothing is persisted.** A restart loses the story and the revision log. Fine for a demo, but the
  "signed artifact" close depends on `/history`, so back it with a file before the event.
- **The model still occasionally copies itself.** Given a wide gap it will restate a sentence it can
  already see, in the prompt or pinned beside it. Detected and rejected, not prevented — a copy that
  slips in before detection stays until a later round happens to dissolve that sentence.
- **Cascades are untested at depth.** The ripple queue is exercised by unit-level runs but no drive has
  yet pushed a phrase into paragraph 1 of six and watched it travel.
- **Idle rounds are rejected more often than weave rounds** (see above). Harmless but wasteful.
- **No moderation.** The pitch invites the room to be weird; there is no filter, so the safeguard is a
  human at the projector.

## Next

1. **Projector UI** — fork `frontend/index.html`, which already colours each canvas position by how long
   it has held. Contributor colours key off the `id` in the `pins` event; the queue depth belongs on
   screen, where it reads as theatre rather than as a limitation.
2. **Persist the story** — a JSONL of revisions written as they commit, and reload on start.
3. **Phone submit page** behind Tailscale Funnel.
4. **Nano Banana** on each `commit`, running concurrently with the next round and never blocking it.
