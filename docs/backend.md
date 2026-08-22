# DiffusionGemma backend — API contract

`dg_openai_server.py` is a stdlib-only HTTP shim in front of
`llama-diffusion-gemma-visual-server`, which holds the GGUF resident and speaks a line protocol over
stdin/stdout. The shim exposes an OpenAI-compatible surface plus one endpoint that has no OpenAI
equivalent: the raw denoising canvas.

    ./run-server.sh          # http://<tailscale-ip>:8080/v1
    frontend/start-ui.sh     # http://<tailscale-ip>:8081/

| | |
|---|---|
| model id | `diffusiongemma-26B-A4B-it` |
| canvas | **256 tokens per block** (`diffusion.canvas_length`, fixed by the model) |
| context | `MAXTOK`, auto-sized at load; **32768** on this box |
| bind | Tailscale IP only — `run-server.sh` refuses to start without one, and never binds `0.0.0.0` |
| env | `DG_HOST`, `DG_PORT` (8080), `NGL` (99), `DG_KEEP_CHANNELS`, `MAXTOK`, `FA` |
| CORS | `Access-Control-Allow-Origin: *` on every response; `OPTIONS` returns 204 |

## Concurrency

**The backend is single-threaded and synchronous.** `Backend.lock` serialises every request; a second
caller blocks until the first finishes. Generation cannot be cancelled — a client that disconnects
mid-stream has its remaining records drained before the lock is released, and the `BEGIN <req>` ack
resynchronises the pipe so a hung-up client can't leave every later reply one request stale.

Anything fanning out to multiple users must queue and batch its own work. Do not issue one request
per user action.

---

## `GET /health`, `GET /healthz`

```json
{ "status": "ok", "model": "diffusiongemma-26B-A4B-it", "maxtok": 32768 }
```

`200` while the model subprocess is alive, `503` with `"status": "backend_dead"` once it isn't.

## `GET /v1/models`, `GET /models`

Standard OpenAI list shape, one entry, `owned_by: "google/unsloth"`.

---

## `POST /v1/chat/completions`

OpenAI-compatible. Non-standard extensions are the `eb_*` knobs, `seed`, and `pins`.

```jsonc
{
  "messages": [ {"role": "system"|"user"|"assistant", "content": "..."} ],  // required, non-empty
  "max_tokens": 512,          // or max_completion_tokens; default 2048. CEIL'D TO WHOLE 256-BLOCKS
  "stream": false,
  "seed": 11,                 // omitted -> derived from wall clock

  // entropy-bound decoder, per request; omit any key to keep the model's own default
  "eb_max_steps": 48,         // hard cap on denoising steps per block
  "eb_entropy_bound": 0.1,    // positions accepted per step; higher crystallises faster
  "eb_t_max": 0.8,            // temperature at the first step
  "eb_t_min": 0.4,            // temperature at the last step
  "eb_confidence": 0.005,     // stop once mean canvas entropy drops below this
  "eb_stability": 1,          // steps the argmax canvas must hold to count as stable

  "pins": [ ... ]             // see "Pinning" below
}
```

`content` may be an OpenAI multipart array; text parts are concatenated and everything else dropped.

Response adds `x_diffusion_stats` (see [Stats](#stats)) alongside the usual `choices`/`usage`.
Reasoning, when the model closed its thought channel, comes back as `message.reasoning_content`.

Streaming emits `chat.completion.chunk` deltas carrying `content` and/or `reasoning_content`, a final
chunk with `finish_reason: "stop"`, then `data: [DONE]`. **Deltas are derived from the backend's
cumulative `C` records, not from canvas frames** — frames mutate in place and would emit garbled text.

### The thought channel

The model wraps reasoning in `<|channel>thought` … `<channel|>`. The closing marker is frequently
never emitted, because the model uses its whole canvas thinking; in that case the shim treats the
thought tail as the answer rather than handing back an empty string. Set `DG_KEEP_CHANNELS=1` to
disable the split and get the raw text with markers intact.

There is **no way to turn the thought channel off from the prompt.** The chat template's generation
prompt is a bare `<|turn>model\n`; the model opens the channel on its own, from training. See
`docs/progress.md` for the eight approaches that were measured and failed.

---

## `POST /v1/diffusion/stream`

SSE. Deliberately not OpenAI-shaped — chat-completions has no concept of "the canvas changed in
place". Same request body as above, plus:

```jsonc
{ "frame_every": 1 }    // emit every Nth denoising step; first and last step of a block always sent
```

Events, one JSON object per `data:` line, terminated by `data: [DONE]`:

```jsonc
{"type":"start",  "block_tokens":256, "n_blocks":3, "seed":11, "model":"...", "eb":{...}}
{"type":"pins",   "block":0, "spans":[{"pos":0,"len":16,"text":"...","id":"story"}]}
{"type":"frame",  "block":0, "step":4, "total":24, "tokens":["Eli","as"," knelt", ...]}
{"type":"commit", "block":0, "reasoning":"...", "answer":"...", "raw":"..."}
{"type":"stats",  "prompt_n":"348", "predicted_n":"256", ...}
{"type":"done"}
{"type":"error",  "message":"..."}
```

- **`frame`** is the live canvas: one string per canvas position, `n_blocks × 256` positions total.
  It is **not monotonic** — the entropy-bound decoder has no mask token, so every position holds a
  real argmax token from step 0 and unaccepted positions are *renoised* each step. Clients visualise
  per-position **change**, not mask→token. Derive stability by diffing consecutive frames.
- **`pins`** reports where each pinned span actually landed after tokenization. Emitted **before that
  block's first frame**, so a UI can colour the spans from step 0.
- **`commit`** is authoritative text; `raw` is cumulative across blocks, `answer`/`reasoning` are the
  thought-channel split of it.
- **`error`** is terminal for the request but the stream still closes cleanly with `[DONE]`.

---

## `POST /v1/tokenize`

Not an OpenAI endpoint. Pin layout is computed client-side, but only the backend owns the vocab, so
this is how a planner learns how many positions a span will occupy. Same tokenizer path the pins take.

```jsonc
// request
{ "texts": ["Elias knelt on the wet stones.", " a stolen accordion"], "special": false }

// response
{ "tokens": [["E","lias"," knelt"," on"," the"," wet"," stones","."], [" a"," stolen"," accordion"]],
  "counts": [8, 3] }
```

`special` matches the pin field of the same name: leave it `false` for anything a user typed. Note the
leading spaces in the pieces — a mid-sentence phrase must carry its leading space or it will fuse with
the previous word. This runs through the same single-threaded lock as generation, so a planner should
batch its `texts` into one call and cache the results rather than measuring spans one at a time.

## Pinning

`pins` freezes chosen canvas positions to caller-supplied text for the whole denoise. Pinned positions
are never renoised and contribute zero to the entropy bound, so the free positions get exactly the
budget they would have had — they simply reweave around the pin. **Attention here is non-causal, so
canvas text *before* a pin reacts to it too.** That is the capability an autoregressive model does
not have.

```jsonc
"pins": [
  { "pos": 0,   "text": "Elias knelt on the wet stones.", "id": "story" },
  { "pos": 110, "text": " a stolen accordion",            "id": "judge-red" }
]
```

| field | type | meaning |
|---|---|---|
| `pos` | int | canvas position `0..255` where the span starts |
| `text` | str | tokenized server-side. **Include the leading space** for a mid-sentence phrase — that space belongs to the first token |
| `block` | int | which 256-token block, default `0`. Pins for other blocks are ignored |
| `id` | str | opaque; echoed back in the `pins` event so a UI can attribute the span |
| `special` | bool | default `false`. Opt-in to parsing control tokens in `text` — **trusted callers only** |

Rules and failure modes:

- Spans are laid down in array order; a later pin **overwrites** an earlier one on the same position.
- A span running past position 255 is **clipped**, not rejected. `len` in the `pins` event reports how
  many tokens actually landed; `len: 0` means the whole span fell off the canvas.
- A pin can still be lost to `trim_canvas` if the block ends (eog, or a repetition loop) before
  reaching it. Check the committed text, not just the `pins` event.
- With `special: false` a submission **cannot** inject `<|channel>` or an eog token. Never set
  `special: true` on text a user typed.
- Malformed entries are dropped silently rather than failing the request.
- A run with **no pins is bit-identical to before the feature existed** — the RNG stream is unchanged
  and the convergence test still averages entropy over all 256 positions.

### What pinning is actually for

Generating into an empty canvas makes this model write a planning outline; it does that regardless of
prompt (measured — see `docs/progress.md`). Pinning the *existing prose* into the canvas with gaps
leaves no room for an outline, and the model writes only the connective tissue. That is both the fix
for the outline behaviour and the mechanism The Loom runs on.

---

## Stats

`x_diffusion_stats` / the `stats` event. **All values are strings.**

| key | meaning |
|---|---|
| `prompt_n` | prompt tokens |
| `predicted_n` | committed canvas tokens |
| `prompt_prepare_ms` | host template + tokenize. Not a GPU prefill, so there is no prompt tok/s |
| `wall_ms` | the generation loop the caller waited on (compute **+** frame emission) |
| `decode_ms` | `wall_ms` minus host visualization overhead ≈ real model compute |
| `blocks`, `steps` | blocks run, denoising steps summed across them |
| `canvas`, `n_ctx` | 256, and the resolved `MAXTOK` |
| `eb_*` | the entropy-bound values actually used |

Frame emission is pure host overhead inside the decode loop, which is why a visualization-inclusive
tok/s looks ~10× slow. Use `decode_ms` when comparing model speed.

## Measured latency

Jetson Thor, Q8_0, `NGL=99`, one 256-token block, adaptive stop usually firing around 16–21 steps:

| story length in prompt | wall | per step |
|---|---|---|
| 114 tok | 5.4 s | 336 ms |
| 639 tok | 6.0 s | 374 ms |
| 1839 tok | 7.6 s | 472 ms |
| 3639 tok | 10.1 s | 629 ms |

`eb_max_steps` is the biggest single lever: 48 → 6.8 s, 12 → 4.3 s, 8 → 2.9 s (and at 8 the canvas
often has not converged). Three blocks cost ~17 s. A gap-filled single-block reweave costs **~6 s**.

## Errors

```json
{ "error": { "message": "...", "type": "invalid_request_error", "code": null, "param": null } }
```

`400` bad JSON or empty `messages` · `404` unknown path · `500` `server_error` for backend failures.

Backend-originated messages worth handling by name:

- `toolong <needed> <budget>` — prompt + canvas exceeds `MAXTOK`. Trim the story, don't retry.
- `backend process is dead` / `backend died mid-request` — the subprocess exited; restart the server.
- `backend acked a different request; stream desynchronised` — should not happen; restart.
