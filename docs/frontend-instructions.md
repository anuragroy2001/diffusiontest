# Frontend instructions — The Loom

You're building the UI for **The Loom**: a story projected on a big screen that a whole room writes
together in real time. Someone scans a QR code, types a phrase, and it gets pinned into the story as
frozen text — the model re-denoises the connective tissue around it live, in colour, on the projector.
Nothing restarts. It absorbs.

This doc describes only what's actually running right now — the backend and its API. The frontend itself
is expected to change a lot as you build it; this isn't a spec for the UI, just what it needs to connect
to.

The current input path is a phone text form, not voice, and the only visual is the text canvas itself —
there's no image-generation layer yet. Build against the API below as it exists today.

```
Phone (QR code) → POST /submit  →  Loom server (owns the story, drives rounds)
                                          │
                                          ▼
                                   DiffusionGemma (pinned-canvas reweave)
                                          │
                                          ▼
                                Projector ← SSE /stream (frames, commits, colours)
```

---

## 1. Two surfaces to build

1. **Submit page** (phone, via QR code) — a text box + optional name field, posting to `/submit`. Small,
   fast, works on a phone browser. Show the contributor their assigned colour after submitting so they
   can spot their words on the big screen.
2. **Projector view** (the big screen) — connects to `/stream` (SSE), renders the story, colours each
   contributor's spans in their colour, animates the live denoise, and shows queue depth ("7 phrases
   waiting to be woven" — this is meant to read as theatre, not a limitation). At the end of the demo it
   also scrubs through `/history` as a time-lapse.

There's an existing raw-canvas UI at `frontend/index.html` worth forking for the *rendering* half — it
already colours canvas positions by how many steps a token has held stable (`held`), which is the same
"freeze in place, colour by change" visual The Loom needs. **But** it currently talks directly to the raw
model backend on `:8080` (`/v1/diffusion/stream`) with hand-built prompts/pins. For The Loom, don't call
`:8080` yourself — talk to the Loom server on `:8082` instead; it owns pin planning, contributor colours,
the story, and round-rejection logic. Reuse `index.html`'s token-coloring/animation code, not its
networking.

---

## 2. Server connection

Right now the Loom server is running at:

```
http://192.168.100.69:8082
```

(Same wifi network as this laptop. If it's restarted or the laptop's IP changes, ask for the new
`http://<ip>:8082` — it prints the URL on startup.)

CORS is open on every response, no auth. No dependencies needed to build the frontend — plain
fetch/EventSource is enough (see `frontend/index.html` for a zero-build, single-file precedent).

---

## 3. API contract

Full spec: [`loom/docs/api.md`](../loom/docs/api.md). Summary of what you need:

### `POST /submit` — the phone form

```jsonc
// request
{ "text": "a stolen accordion",   // required, whitespace collapsed, max 120 chars
  "name": "Ada",                  // optional, max 24 chars
  "id": "8f2c...",                // optional: pass back the id you were given last time to keep your colour
  "target": 0 }                   // optional paragraph index; omit for "wherever is live"

// 200
{ "queued": true, "depth": 3,
  "contributor": { "id": "8f2c...", "name": "Ada", "colour": "#f07178" } }
```

- Store `contributor.id` (e.g. `localStorage`) and send it back on future submissions from the same
  device — that's how you keep the same colour instead of getting a new one each time.
- `400` if `text` is missing/empty. `429` if the global queue is full or this contributor already has 3
  pending phrases — show that as "hang on, your last one hasn't landed yet," not an error.
- There's no profanity filter by design. Moderation is a human with the projector remote, not your job.

### `GET /state` — snapshot (poll, or just use `/stream`'s first event)

```jsonc
{ "paragraphs": ["...", "..."],
  "live": 2,                // index of the paragraph currently being rewoven
  "round": 17,
  "busy": true,
  "pending": [ {"text": "...", "contributor": "8f2c...", "at": 1.7e9, "target": null} ],
  "contributors": { "8f2c...": {"id": "...", "name": "Ada", "colour": "#f07178"} },
  "max_per_round": 2 }
```

### `GET /stream` — SSE, this is what drives the projector

Opens with a full `state` event so a client connecting mid-round renders immediately. Keepalive comment
every 15s (ignore it).

| event | payload | what to do with it |
|---|---|---|
| `state` | everything from `/state` | initial render |
| `queue` | `depth`, `pending` | update the "N phrases waiting" indicator |
| `round_start` | `round`, `kind`, `block`, `seed`, `used`, `canvas`, `notes`, `submissions[]` (with contributor `colour`), `pins[]` | a reweave is starting — show which submissions are entering this round |
| `pins` | `block`, `spans[]` | **relayed from the model.** Where each span landed once tokenized — colour these positions from step 0, before any frames arrive |
| `frame` | `block`, `step`, `total`, `tokens[]` | **the live denoise**, ~18 events per round, one string per canvas position (256 per block). **Non-monotonic** — colour by *change between consecutive frames*, not by "mask filling in." This is the only droppable event; don't worry about missing one |
| `commit` | `round`, `block`, `text`, `spans`, `kind` | **authoritative.** The paragraph actually changed to this. Never construct story text from `frame` events — only from `commit` |
| `round_rejected` | `round`, `block`, `reason` | the round was thrown away, story unchanged — maybe flash something subtle, not an error state |
| `split` | `live`, `paragraphs`, `state` | a paragraph got trimmed/merged — re-render from the included `state`, and note `live` may have moved |
| `round_end` | `round`, `state` | round finished, general resync point |
| `error` | `message` | round failed, story unchanged, any phrases went back on the queue |

### `GET /history` — for the time-lapse close

```jsonc
{"revisions": [
  { "n": 4, "at": 1.7e9, "block": 1, "kind": "weave",
    "before": "...", "after": "...", "seed": 1004,
    "submissions": [ {"text": "a stolen accordion", "contributor": "8f2c..."} ],
    "spans": [ {"pos": 86, "len": 3, "text": " a stolen accordion", "id": "8f2c..."} ] }
]}
```

Every round, `before`/`after` in full. This is the "written by everyone in this room and no one" close —
scrub through it at the end of the demo.

### `GET /health`

`{"status": "ok", "round": 17, "api": "http://..."}` — poll this to show a "the loom is alive" indicator
if you want one; not required.

---

## 4. Things that will bite you if you assume otherwise

- **A canvas is one paragraph (~256 tokens, ~190 words).** The "live" reweave region is always exactly
  one paragraph; earlier committed paragraphs don't ripple live, they only change when a later round's
  cascade reaches them.
- **Paragraph indices move.** A `split` can insert or merge paragraphs. Re-read `/state` (it's included
  in the `split` payload) rather than caching indices.
- **`frame` is lossy by design** — never derive story text from it, only from `commit`.
- **One model, one lock, globally.** Every submission funnels through the same single-threaded reweave.
  Rounds are capped at ~2 submissions each (`max_per_round`) and run every ~25s even when idle
  (`LOOM_IDLE_ROUND_S`) so the story keeps breathing. The rest of the queue waits its turn — again, put
  that queue depth on screen, it's part of the show.
- **Nothing is persisted.** If the Loom server restarts, the story and revision log are gone. Don't build
  against an assumption of durability across restarts.
- Contributor colour is assigned once server-side and is stable as long as you keep sending the same
  `id` back — don't generate your own colours client-side.
