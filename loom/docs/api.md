# The Loom — API contract

`loom/server.py` owns the story and drives the rounds. It does **not** own the model: it talks to the
DiffusionGemma shim over HTTP (`../../docs/backend.md`) and relays the denoising frames straight through
to the projector.

    python3 loom/server.py        # binds the Tailscale IP only, same policy as run-server.sh

| | |
|---|---|
| default port | `8082` (`LOOM_PORT`) |
| model API | `http://100.70.13.60:8080` (`DG_API`) — checked at startup, refuses to run without it |
| bind | Tailscale IP, resolved at launch (`LOOM_HOST` to override; `0.0.0.0` is refused) |
| CORS | open on every response; `OPTIONS` returns 204 |

Other env: `LOOM_MAX_PER_ROUND` (2), `LOOM_MAX_PENDING` (60).

---

## `POST /submit`

The phone endpoint. Queues a phrase and returns the contributor's colour.

```jsonc
// request
{ "text": "a stolen accordion",   // required; whitespace collapsed, trimmed to 120 chars
  "name": "Ada",                  // optional, 24 chars
  "id": "8f2c...",                // optional: the id from a previous reply, kept in localStorage
  "target": 0 }                   // optional paragraph index; omit for "wherever is live"

// 200
{ "queued": true, "depth": 3,
  "contributor": { "id": "8f2c...", "name": "Ada", "colour": "#f07178" } }
```

A contributor's colour is assigned once and never changes — it is how the room recognises its own words
on the screen. Send back the `id` you were given and you keep your colour; omit it and you are a new
contributor.

| status | when |
|---|---|
| `400` | `text` missing or empty |
| `429` | the queue is full (`LOOM_MAX_PENDING`), or you already hold 3 pending phrases |

Rate limiting is per contributor id, which is client-supplied — it stops enthusiasm, not an attacker.
There is no profanity filter; the pitch invites the room to be weird, so moderation is a human with the
projector remote.

## `GET /state`

```jsonc
{ "paragraphs": ["...", "..."],   // the whole story, in order
  "live": 2,                      // index of the paragraph currently being rewoven
  "round": 17,
  "busy": true,
  "pending": [ {"text": "...", "contributor": "8f2c...", "at": 1.7e9, "target": null} ],
  "contributors": { "8f2c...": {"id": "...", "name": "Ada", "colour": "#f07178"} },
  "max_per_round": 2 }
```

## `GET /history`

`{"revisions": [...]}` — every round, for the time-lapse close. Each entry carries `before` and `after`
in full, plus `seed`, so any round can be replayed exactly.

```jsonc
{ "n": 4, "at": 1.7e9, "block": 1, "kind": "weave",   // weave | ripple
  "before": "...", "after": "...", "seed": 1004,
  "submissions": [ {"text": "a stolen accordion", "contributor": "8f2c..."} ],
  "spans":       [ {"pos": 86, "len": 3, "text": " a stolen accordion", "id": "8f2c..."} ] }
```

## `GET /stream`

SSE for the projector. Opens with a full `state` event, so a client connecting mid-round renders
immediately instead of waiting for the next commit. A `: keepalive` comment goes out every 15 s.

| event | payload | notes |
|---|---|---|
| `state` | everything from `/state` | first event on every connection |
| `queue` | `depth`, `pending` | a phrase arrived, or a failed round put one back |
| `round_start` | `round`, `kind`, `block`, `seed`, `used`, `canvas`, `notes`, `submissions[]`, `pins[]` | `submissions` carry their contributor `colour` |
| `pins` | `block`, `spans[]` | **relayed from the model** — where each span landed once tokenized. Arrives before the first frame, so spans can be coloured from step 0 |
| `frame` | `block`, `step`, `total`, `tokens[]` | **relayed from the model.** One string per canvas position, ~18 per round. Non-monotonic: colour by *change*, not by mask→token |
| `commit` | `round`, `block`, `text`, `spans`, `kind` | the paragraph changed |
| `round_rejected` | `round`, `block`, `reason` | the weave was thrown away; the story is unchanged |
| `split` | `live`, `paragraphs`, `state` | the live paragraph was trimmed back and the overflow settled |
| `round_end` | `round`, `state` | |
| `error` | `message` | the round failed; the story is unchanged and any phrases went back on the queue |

Each subscriber has its own 256-deep queue. **`frame` is the only droppable event** — a projector that
falls behind skips frames rather than delaying a commit. Everything else evicts the oldest queued frame
to make room, so no client ever misses a commit.

## `GET /health`

`{"status": "ok", "round": 17, "api": "http://..."}`

---

## What a round does

1. Drain up to `MAX_PER_ROUND` phrases that all want the same paragraph. A canvas is one paragraph, so
   phrases aimed elsewhere keep their place in the queue.
2. `plan_weave()` lays the paragraph back into the 256-position canvas with gaps, drops the phrases into
   those gaps, and protects every phrase the room has already landed.
3. Stream the denoise from the model, relaying frames to the projector.
4. `tidy()` the result, then accept or reject it (below).
5. Commit, log the revision, and queue a ripple for every paragraph after this one.
6. Trim the live paragraph back if it has outgrown the canvas.

## When a round is thrown away

The story is never left worse than it was found. A round is rejected, and its phrases requeued, if:

- **fewer than 75% of pinned story spans survive** — a canvas that trimmed early or a model that
  wandered. Committing that would silently delete the room's story.
- **a run of 12+ words was newly duplicated** — either lifted from a neighbouring paragraph or repeated
  twice within this one. Given a wide gap, the model will fill it by restating a sentence it can already
  see. Runs already present before the round are ignored: flagging those would reject every future round
  forever and freeze the paragraph at the moment it most needs rewriting.

A rejected round costs about six seconds, so rejecting is nearly free and always the right call.

## Things a client must not assume

- **The model is single-threaded.** Every round, and every `/v1/tokenize` call the planner makes, goes
  through one lock. Two Looms against one model will interleave badly.
- **Paragraph indices move.** A split can insert a paragraph or merge overflow into the one behind. Use
  `target` on `/submit` only for a deliberate cascade, and re-read `/state` after any `split` event.
- **`frame` events are lossy by design.** Never derive story text from them; use `commit`.
- **Nothing is persisted.** Restarting the server loses the story and the revision log.
