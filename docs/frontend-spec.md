# The Loom — frontend spec

Backend is currently hosted at:

```
http://192.168.100.69:8082
```

(Same wifi network as the projector laptop. If it's restarted or the IP changes, get the new
`http://<ip>:8082` — the server prints it on startup.) CORS is open, no auth — plain `fetch` /
`EventSource` is enough, no SDK needed.

Two independent apps to build. They never talk to each other directly — both only talk to the backend
above.

---

## Surface 1 — Submit page (phone, reached via QR code)

### What it should do

1. Show a text input for a phrase and an optional name input.
2. On submit, `POST /submit`. On success, show the contributor their assigned colour and confirm the
   phrase is queued ("in the loom now" / depth in queue). On failure, show why (see error handling
   below) and let them retry without losing what they typed.
3. Remember the contributor's `id` and `colour` locally (e.g. `localStorage`) and send `id` back on
   every subsequent submission from the same device, so returning contributors keep the same colour
   instead of being treated as a new person each time.
4. Enforce input limits client-side before hitting the network: phrase ≤ 120 chars, name ≤ 24 chars,
   phrase non-empty. The backend enforces these too, but catching it client-side avoids a round trip.
5. Let a contributor submit again immediately after a successful submit — don't lock the form. The
   backend caps a contributor at 3 pending phrases at once; if they hit that cap, surface it as "your
   last ones haven't landed yet," not a generic error.
6. No profanity filter, no client-side moderation — that's a human with the projector remote, not this
   page's job.

### Endpoint

**`POST /submit`**

```jsonc
// request
{ "text": "a stolen accordion",   // required, trim/collapse whitespace, max 120 chars
  "name": "Ada",                  // optional, max 24 chars
  "id": "8f2c...",                // send back the id from a previous response to keep the same colour
  "target": 0 }                   // omit — leave targeting a specific paragraph out of this surface

// 200 response
{ "queued": true, "depth": 3,
  "contributor": { "id": "8f2c...", "name": "Ada", "colour": "#f07178" } }
```

| status | meaning | what to show |
|---|---|---|
| `200` | queued | contributor's colour + confirmation, queue depth if you want |
| `400` | `text` missing/empty | inline validation error, don't submit |
| `429` | queue full, or this contributor already has 3 pending | "hang on, your last one hasn't landed yet" |

---

## Surface 2 — Projector view (the big screen)

### What it should do

1. **On load**, connect to `GET /stream` (SSE) and render from its first event (a full `state`) —
   don't separately call `/state` first and then open the stream; the stream already opens with a
   complete snapshot so a client connecting mid-round renders immediately.
2. **Render the story** as paragraphs, with the currently-live paragraph (the one being rewoven this
   round) visually distinguished from settled ones.
3. **Colour each contributor's words** in their assigned `colour` wherever their submitted text appears
   in the story, using the `id` on spans to attribute them. This is the core "the room recognizes its
   own words" mechanic — a contributor's colour never changes across rounds.
4. **Animate the live denoise** while a round is running: as `frame` events arrive, redraw the live
   paragraph's canvas position-by-position, highlighting positions that just changed vs. positions that
   have been stable for a while (mirrors the "renoise vs. frozen" visual in `frontend/index.html`).
   Frames are delivered ~18 times per round and are **non-monotonic** — diff consecutive frames to
   detect change, don't assume progressive fill-in.
5. **Commit visibly**: when a round lands, replace the live paragraph's text with the authoritative
   `commit` text (never assemble displayed story text from `frame` data).
6. **Show the queue as theatre**: a visible "N phrases waiting to be woven" indicator, driven by `queue`
   events (and the `pending` array in `state`/`round_start`). This is meant to read as anticipation, not
   as the system struggling.
7. **Handle a rejected round gracefully**: on `round_rejected`, the story is unchanged and the reason is
   informational — a brief, low-key flash is enough, not an error banner.
8. **Handle paragraph restructuring**: on `split`, re-render fully from the `state` included in that
   event (paragraph indices can change — a paragraph may be inserted or merged).
9. **Recover from a transient backend error**: on `error`, the story is unchanged and any in-flight
   phrases were already requeued server-side — show the message briefly, keep listening on the same
   stream.
10. **Show the illustrated world**: render the latest generated still (per paragraph `block`) alongside
    the text, and **crossfade** to a new one over ~400ms when an `image` event arrives — don't hard-cut.
    This is a companion visual, not a video feed: a new still lands roughly once per committed round
    (~6–15 sec later, after the round's `commit`), not continuously. If an `image` event's `round` is
    older than the one already shown for that `block`, drop it — a slow generation can be overtaken by a
    newer round before it arrives.
11. **Time-lapse close**: a way to trigger (e.g. a key press for the human running the projector) a
    scrub through `GET /history`, replaying `before → after` per round as the demo's closing beat
    ("written by everyone in this room and no one").
12. If the SSE connection drops, reconnect and re-render from the fresh `state` event the reconnect
    delivers — don't try to resume mid-stream.

### Endpoints

**`GET /stream`** — SSE, the primary driver for everything above.

| event | payload | drives |
|---|---|---|
| `state` | full snapshot (`paragraphs`, `live`, `round`, `busy`, `pending`, `contributors`, `max_per_round`) | initial render, and full resync after `split` |
| `queue` | `depth`, `pending` | queue-depth indicator |
| `round_start` | `round`, `kind`, `block`, `seed`, `used`, `canvas`, `notes`, `submissions[]` (incl. contributor `colour`), `pins[]` | announce which submissions are entering this round |
| `pins` | `block`, `spans[]` | colour submitted spans from step 0, before frames arrive |
| `frame` | `block`, `step`, `total`, `tokens[]` | live denoise animation (droppable — skip if behind, never block on it) |
| `commit` | `round`, `block`, `text`, `spans`, `kind` | authoritative paragraph text — the only source of truth for displayed story |
| `image` | `round`, `block`, `url` (or `data`, a data URI) | a new illustration for that paragraph, generated from the committed text — droppable, crossfade in, ignore if older than the `round` already shown for that `block` |
| `round_rejected` | `round`, `block`, `reason` | brief, low-key rejection indicator |
| `split` | `live`, `paragraphs`, `state` | full re-render, paragraph indices may have moved |
| `round_end` | `round`, `state` | general resync point after a round finishes |
| `error` | `message` | brief transient-error indicator |

**`GET /state`** — same shape as the `state` SSE event. Not required if the stream is connected (its
first event already delivers this), but useful for a secondary display, debugging, or a lightweight
health/status widget that doesn't want a persistent connection.

```jsonc
{ "paragraphs": ["...", "..."],
  "live": 2,
  "round": 17,
  "busy": true,
  "pending": [ {"text": "...", "contributor": "8f2c...", "at": 1.7e9, "target": null} ],
  "contributors": { "8f2c...": {"id": "...", "name": "Ada", "colour": "#f07178"} },
  "max_per_round": 2,
  "images": { "0": {"round": 12, "url": "..."}, "2": {"round": 17, "url": "..."} } }
```

`images` is keyed by paragraph `block` and holds only the latest still per block — this is what lets a
client that connects mid-stream (or reconnects) show the current illustrations immediately instead of
waiting for the next round's `image` event. A block with no entry yet just shows nothing/a placeholder.

**`GET /history`** — for the time-lapse close.

```jsonc
{"revisions": [
  { "n": 4, "at": 1.7e9, "block": 1, "kind": "weave",
    "before": "...", "after": "...", "seed": 1004,
    "submissions": [ {"text": "a stolen accordion", "contributor": "8f2c..."} ],
    "spans": [ {"pos": 86, "len": 3, "text": " a stolen accordion", "id": "8f2c..."} ] }
]}
```

**`GET /health`** *(optional)* — `{"status": "ok", "round": 17, "api": "http://..."}`. Fine to poll for
a small "the loom is alive" indicator; not required for core functionality.

---

## Constraints both surfaces must respect

- **A canvas is one paragraph** (~256 tokens, ~190 words) — only the `live` paragraph animates per
  round; earlier paragraphs update only when a later cascade reaches them.
- **One model, one global lock** — rounds are capped at `max_per_round` submissions and the whole room
  shares one queue. Don't build per-user private state that implies otherwise.
- **Nothing is persisted server-side** — a backend restart loses the story and history. Don't design a
  UX that assumes the story survives a refresh of the *server*, only of the client.
- **Contributor colour is always server-assigned** — never generate or override it client-side.
- **Illustrations are a still-image companion, not video** — Nano Banana (Gemini 3.7 Flash) is a hosted
  call with real per-image latency and cost, triggered once per committed round rather than continuously.
  Don't build a UI that implies a live camera-like feed; a crossfade between stills is the right amount
  of motion.
