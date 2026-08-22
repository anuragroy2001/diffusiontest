# Plates — the image side of The Loom

One **plate** per story beat. A plate is made by *editing the previous plate*, never by generating a
fresh image, and that single choice is what the whole visual effect rests on.

    export GEMINI_API_KEY=...
    python3 loom/plates/cli.py --serve "A lone knight travels through an ancient forest"

`--serve` prints a `http://127.0.0.1:8090` URL and keeps it live: leave the page open and every plate
you render appears on its own and plays its transition. (Without `--serve` the page still works, but
`file://` cannot poll the manifest, so you re-run `:view` and reload by hand.)

That renders the opening plate and drops you at a prompt. Every line you type is an edit — `he's
actually a robot`, `the year is 3000` — which is the same shape a Loom round has. `:view` writes a page
that cross-dissolves the chain so you can judge the morph instead of guessing at it.

    plate> He is actually an ancient robot, exposed hydraulics, a glowing optical sensor.
      001.png  5.8s  840 KB  [gemini-3.1-flash-lite-image]
    plate> :view

Non-interactive, for scripted comparisons:

    python3 loom/plates/cli.py "..." --edit "..." --edit "..." --out out/compare

| | |
|---|---|
| `gemini.py` | client — `generate()`, `edit()`, model registry, reference re-encoding |
| `cli.py` | the prompt REPL and its commands |
| `viewer.py` | writes a self-contained cross-dissolve page next to the plates |
| `out/` | generated plates, `chain.json`, `index.html`. Gitignored |

Commands: `:view` `:new <prompt>` `:model lite|flash|pro` `:style <text>` `:undo` `:redo` `:ls`
`:where` `:q`

---

## Why editing, not generating

Consecutive edits come back with **near-identical composition** — same trees, same pose, same light,
down to the painted signature in the corner. Only the thing you asked to change changes. So a plain
opacity cross-fade between two plates reads as *the world changing in place*, because the pixels around
the change don't move.

Generate each plate independently and you lose it: the protagonist gets a new face every round and each
transition is a cut. **No video model is involved, and none is needed.** If a transition in the viewer
looks like a cut, the instruction let the composition move — tighten the preservation clause rather than
reaching for LTX.

Four chained edits showed no visible degradation, so sequential-edit drift is not a problem at demo
depth. Worth re-checking if a session ever runs long.

## Model choice is a latency decision

A Loom round is ~6 s (`../../docs/backend.md`). Measured on this key at 1376×768:

| alias | model | generate | edit |
|---|---|---|---|
| `lite` | `gemini-3.1-flash-lite-image` (Nano Banana 2 Lite) | **3.8 s** | **5.5–5.8 s** |
| `flash` | `gemini-3.1-flash-image` (Nano Banana 2) | 11.2 s | — |
| `pro` | `gemini-3-pro-image` (Nano Banana Pro) | 17.7 s | — |

Only `lite` keeps up with the round loop. `pro` is for the final plate, when nothing is waiting on it.

Note that **`gemini-3.7-flash` cannot generate images** — it is on the key and it is a text model. The
image models are the Nano Banana family above.

## Everything is an oil painting

`STYLE` establishes the medium on the opening plate — oil on canvas, visible brushstrokes and impasto,
canvas weave showing through, painterly edges. Every later plate is an edit of that one, so if the first
plate is not convincingly a painting, nothing downstream will be either.

`STYLE_HOLD` re-asserts it on **every edit**, and that is not redundant. An edit instruction describes
*content* ("he is a robot"), and the model will render new content in its own default idiom — chrome,
neon and water pull especially hard toward photorealism. State the medium once at the start and the
chain drifts out of paint within a few beats; state it every time and it holds. Verified on the worst
case: polished chrome, bright neon signage and a full underwater scene, and the final plate still had
brushstroke texture in the roots, painterly bloom on the neon, and a painted signature in the corner.

`--no-style-hold` drops the reassertion so you can watch the drift happen.


## The three defaults that were measured

- **Re-encode the reference as JPEG** (`as_reference`). Sending the ~900 KB PNG back as the edit
  reference cost 8.7–12.7 s per edit; the same picture as JPEG at 1024px lands at 5.5–5.8 s. Resolution
  is almost free below that — 1376px and 768px both came back at ~7 s in the PNG-era probe — so this is
  about the encoder, not the pixel count. Downscale for bandwidth, not for speed.
- **The preservation clause** (`PRESERVE` in `cli.py`), appended to every edit. It is what holds camera,
  pose and layout still between plates. Run with `--no-preserve` to watch the chain fall apart.
- **The style hold** (`STYLE_HOLD`), above. Kept short so it does not compete with the instruction
  itself for attention.

## Wiring it to the round loop (not built yet)

Render on **`round_start`**, not on `commit`. `loom/server.py` publishes `round_start` carrying the
submission text — the delta instruction, `"he's actually a robot"` — a full round *before* the paragraph
commits. Firing the edit there means the plate is ready roughly when the text lands, which hides the
entire image latency. A rejected round costs one discarded plate, which is nothing.

Then treat plates the way `server.py` already treats canvas frames: **droppable**. If a render is in
flight when the next commit arrives, skip it and render the latest state rather than queuing, or the
image walks steadily further behind the story.

You cannot render per denoise frame — frames arrive every ~330 ms and an image takes seconds. The
in-between imagery is the cross-dissolve, not a generated still.

## Keys

`GEMINI_API_KEY` comes from the environment and is never written into the repo; `out/` is gitignored.
Nothing here binds a port, so the Tailscale-only policy in `run-server.sh` is unaffected — this is
outbound only.
