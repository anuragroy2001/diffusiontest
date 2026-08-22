# DiffusionGemma denoising UI

Static single-file UI (no build, no deps) that watches the model denoise in real time.

    ./start-ui.sh          # tmux window 'diffusiongemma:dg-ui', prints the URL
    ./start-ui.sh stop
    ./start-ui.sh attach

UI on `:8081`, API on `:8080`, both bound to the Tailscale IP only.

## What you're looking at

The entropy-bound decoder has **no mask token**. Every one of the 256 canvas positions holds a real
argmax token from step 0; each step the decoder accepts the lowest-entropy positions and *renoises*
the rest. So the canvas churns and freezes rather than filling in blanks.

The UI colours each position by how many consecutive steps its token has been unchanged — the same
`held` signal the decoder uses to decide it has converged:

| colour        | meaning                                  |
|---------------|------------------------------------------|
| orange block  | changed this step (renoised)             |
| fading grey   | unchanged 1-3 steps                      |
| green         | frozen (unchanged 4+ steps)              |
| purple        | frozen, inside the `<\|channel>` thought  |

When a block commits, its trimmed text freezes into the prefix and the next block starts a fresh
canvas after it. `frozen N/256` and the step bar track convergence; generation adaptive-stops when
the canvas is stable and confident, so it usually ends before `steps`.

## On a phone

The canvas gets the whole screen: the answer/thought/stats panel is dropped (its text still shows in
the canvas as committed output), the settings collapse behind **⚙**, and the canvas starts at 19px.
**A−/A+** resizes the canvas text on any screen and the choice persists per device.

## The endpoint

`POST /v1/diffusion/stream` — SSE, not OpenAI-shaped (chat-completions has no concept of "the canvas
changed in place"). `/v1/chat/completions` still works unchanged for normal clients.

```jsonc
{ "messages": [...], "max_tokens": 512,   // ceil'd to whole 256-token blocks
  "seed": 11, "frame_every": 1,           // render every Nth step
  "eb_max_steps": 48, "eb_entropy_bound": 0.1,
  "eb_t_max": 0.8, "eb_t_min": 0.4, "eb_confidence": 0.005, "eb_stability": 1,
  "pins": [ { "pos": 90, "text": " a stolen accordion", "block": 0, "id": "judge-red" } ] }
```

Events: `start`, `pins {block, spans[]}`, `frame {block, step, total, tokens[]}`,
`commit {reasoning, answer, raw}`, `stats`, `done`, `error`. The `eb_*` keys are the CLI's
`--diffusion-eb-*` flags, made per-request; omit any of them to keep the model's own default.
`stats` echoes the values actually used.

## Pinning text into the canvas

`pins` freezes a phrase at a canvas position for the whole denoise. Pinned positions are never renoised
and cost nothing against the entropy bound, so the free positions get the same budget they would have had
and simply reweave around the pin. Because attention here is non-causal, the canvas text *before* a pin
also reacts to it — which is the thing an autoregressive model structurally cannot do.

| field   | meaning |
|---------|---------|
| `pos`   | canvas position 0-255 where the span starts |
| `text`  | tokenized server-side; **include the leading space** for a mid-sentence phrase, it belongs to the first token |
| `block` | which 256-token block (default 0); pins for other blocks are ignored |
| `id`    | opaque, echoed back in the `pins` event so a UI can attribute the span |

The `pins` event reports where each span actually landed once tokenized —
`{pos, len, text, id}`, emitted before that block's first frame so a UI can colour it from step 0.
`len: 0` means the span ran off the end of the canvas and was dropped. A span can also be lost to
`trim_canvas` if the block ends (eog or a repetition loop) before reaching it.

Submitted text is tokenized with `parse_special=false`, so a submission can never inject a `<|channel>`
marker or an eog token into the canvas.

A run with no pins is bit-identical to before the feature existed: the RNG stream is unchanged (the random
token is still drawn for every position, pinned or not) and the convergence test still averages entropy
over every position when nothing is pinned.

## Knobs worth demoing

- **steps** (`eb_max_steps`, default 48) — hard cap on denoising steps per block; the single biggest
  lever on both animation length and latency. Low values (8-12) give a fast, choppy, dramatic run.
- **entropy** (`eb_entropy_bound`, default 0.1) — how many positions get accepted per step. Higher
  crystallises faster in fewer steps.
- **temp** (`eb_t_max`, default 0.8; the schedule anneals to half it) — lowering it to ~0.5 converges
  in noticeably fewer steps and, in practice, more often finishes the thought channel.
- **tokens** — each 256 is one block. One block often isn't enough for the model to close
  `<|channel>`, in which case the answer pane falls back to showing the thought tail.

## Backend changes this depends on

- `diffusion-gemma-visual-server.cpp`: `F` frames carry a JSON array of per-position pieces (was one
  flat string, which lost token boundaries); per-request `eb_*` overrides; a `BEGIN <req>` ack.
- `dg_openai_server.py`: frame passthrough, the `/v1/diffusion/stream` endpoint, and resync — a
  client that disconnects mid-generation used to leave its records in the pipe and put every later
  reply one request behind.
- `diffusion.cpp`: `diffusion_eb_params::pinned`, the canvas-position freeze that `pins` drives.
  `diffusion-gemma-visual-server.cpp` tokenizes each span and emits the `P` record saying where it
  landed; `dg_openai_server.py` validates `pins` and forwards it on both endpoints.
