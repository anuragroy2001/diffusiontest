"""The Loom — a living story the whole room writes at once.

A story is projected on a screen. Anyone scans a QR code and throws in a phrase; the phrase is pinned
into the diffusion canvas as frozen tokens and the paragraph visibly re-denoises around it. The story
never restarts and never breaks — it absorbs.

Endpoints: 
    POST /submit: Queue a phrase, get a contributor colour
    GET  /state: The whole story, queue, contributors
    GET  /stream: SSE: rounds, canvas frames, commits
    GET  /history: The revision log, for the time-lapse close
    GET  /health: Health check

Run:  python3 loom/server.py
"""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from illustrate import Illustrator  # noqa: E402
from weave import Policy, Submission, Tokenizer, plan_weave, split_sentences, tidy  # noqa: E402

DG_API   = os.environ.get("DG_API", "http://100.70.13.60:8080")
PORT     = int(os.environ.get("LOOM_PORT", "8082"))

MAX_PER_ROUND = int(os.environ.get("LOOM_MAX_PER_ROUND", "2"))   # see the module docstring
MAX_PENDING   = int(os.environ.get("LOOM_MAX_PENDING", "60"))    # queue cap; beyond this /submit is 429
MAX_PER_PERSON = 3                                               # pending phrases one contributor may hold
MAX_PHRASE    = 120                                              # characters
SPLIT_AT      = 185      # tokens the live paragraph is trimmed back to, leaving ~70 canvas positions of
                         # gap. Keeping it NEAR the canvas size is what stops the story running away:
                         # a small live paragraph means enormous gaps, and the model fills enormous gaps
                         # with 150 tokens of new prose, which splits again the next round and never
                         # settles. A nearly-full canvas rewrites the paragraph instead of extending it.
PARA_MAX      = 260      # tokens a frozen paragraph may reach before the overflow starts a new one
ACCEPT_RATIO  = 0.75     # fraction of pinned story spans that must survive for a round to be committed
DUP_RUN       = 12       # words: a verbatim run this long shared with another paragraph is plagiarism,
                         # not prose. Given a wide gap and neighbouring text in the prompt, the model
                         # will happily copy a whole passage across instead of writing a new one.
CONTEXT_PARAS = 1        # prior paragraphs given as prompt. More continuity, but also more to copy.

EB = {"eb_max_steps": 24, "eb_t_max": 0.6, "eb_t_min": 0.3}      # settled in loom/seamtest.py

# The cold open runs once per story, not once per round, so it can afford to spend more than a live
# weave round would: more steps and a lower temperature measurably cut the repetition-loop artifacts
# ("let let ... hours hours") that EB's live-round settings still let through on a bare autoregressive
# completion. Measured live: at 512 tokens ~40% of opens came back with no usable continuation at all --
# the model burns most of the budget in its thought channel and gets cut off before a sentence-ending
# punctuation mark, so _bootstrap_prose has nothing to keep. 768 tokens (3 canvas blocks) cut that to
# ~20%; BOOTSTRAP_SENTENCES then caps the survivors to an opener instead of a full purple-prose
# paragraph.
BOOTSTRAP_EB = {"eb_max_steps": 32, "eb_t_max": 0.5, "eb_t_min": 0.25}
BOOTSTRAP_MAX_TOKENS = 768
BOOTSTRAP_SENTENCES = 3

# Minimal prompt, deliberately. Telling the model to preserve the existing text made it treat the pinned
# spans as quoted material and wrap them in brackets; the pins already guarantee preservation.
SYSTEM = "You are a prose continuation engine. Prose only."

PALETTE = ["#f07178", "#7aa2f7", "#7ec699", "#e0af68", "#c99bf5",
           "#56b6c2", "#ff9e64", "#bb9af7", "#9ece6a", "#f7768e"]

_BOOTSTRAP_BULLET = re.compile(r"^\s*[*\-]\s{2,}.*$", re.M)


def _bootstrap_prose(raw: str) -> str:
    """Extract the story from a cold-open completion. Given only a short phrase to continue from (no
    established narrative), the model reliably plans in a bulleted outline before it writes real prose
    -- the opposite order of a normal round, so unlike `tidy()` this keeps the text AFTER the last
    bulleted line, not before it. Confirmed live: with room to finish (max_tokens=512), good prose
    consistently follows the outline. Markdown emphasis asterisks around that prose are stripped, and
    the result is trimmed to the last complete sentence so a token-limit cutoff mid-clause doesn't ship."""
    matches = list(_BOOTSTRAP_BULLET.finditer(raw))
    tail = raw[matches[-1].end():] if matches else raw
    tail = tail.replace("*", "").strip()
    end = max((tail.rfind(c) for c in ".!?"), default=-1)
    return tail[:end + 1].strip() if end >= 0 else ""


# --------------------------------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------------------------------

@dataclass
class Contributor:
    id: str
    name: str
    colour: str

    def as_json(self) -> dict:
        return {"id": self.id, "name": self.name, "colour": self.colour}


@dataclass
class Pending:
    text: str
    contributor: str
    at: float
    target_id: int | None = None  # paragraph id; None means whichever is live when the round runs
    after: int | None = None      # sentence index within that paragraph to insert behind; None spreads

    def as_json(self) -> dict:
        return {"text": self.text, "contributor": self.contributor, "at": self.at,
                "target_id": self.target_id, "after": self.after}


@dataclass
class Revision:
    """One round, kept so the close can scroll back through every ripple."""
    n: int
    at: float
    block: int
    before: str
    after: str
    seed: int
    kind: str                                 # "weave" | "ripple"
    retro: bool = False                       # a weave round that edited an already-settled paragraph
    submissions: list[dict] = field(default_factory=list)
    spans: list[dict] = field(default_factory=list)   # where the pins landed, from the backend

    def as_json(self) -> dict:
        return {"n": self.n, "at": self.at, "block": self.block, "before": self.before,
                "after": self.after, "seed": self.seed, "kind": self.kind, "retro": self.retro,
                "submissions": self.submissions, "spans": self.spans}


class Bus:
    """Fan-out to projector clients. Each subscriber gets its own bounded queue; a slow client loses
    frames rather than stalling the round, because frames are the one event type where dropping some is
    invisible and blocking on them is not."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[queue.Queue] = []

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=256)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    def publish(self, event: dict, droppable: bool = False) -> None:
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                if droppable:
                    continue
                # Not droppable (a commit, a round boundary): make room by discarding the oldest frame.
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (queue.Empty, queue.Full):
                    pass


# --------------------------------------------------------------------------------------------------
# the loom
# --------------------------------------------------------------------------------------------------

class Loom:
    def __init__(self, api: str = DG_API) -> None:
        self.api = api.rstrip("/")
        self.tok = Tokenizer(api)
        self.bus = Bus()
        self.lock = threading.RLock()

        # The room writes the opening line too -- no fixed premise. The first weave round pins whatever
        # phrases arrive first into this empty paragraph, exactly the "submissions floating in free
        # space" case plan_weave already supports.
        self.paragraphs: list[str] = [""]
        # Opaque, permanent identity for each paragraph -- indices shift as the story grows (a split can
        # insert a new paragraph), but an id, once assigned, is never reassigned or reused. Anything a
        # client wants to reference later (an edit target) should hold the id, not the index.
        self.paragraph_ids: list[int] = [0]
        self._next_pid = 1
        self._pid_index: dict[int, int] = {0: 0}   # paragraph id -> current index
        self.live = 0
        self.pending: deque[Pending] = deque()
        self.contributors: dict[str, Contributor] = {}
        self.history: list[Revision] = []
        # Every phrase the room has ever landed. Re-pinned on later rounds so a rewrite of the sentence
        # holding someone's words cannot quietly delete them.
        self.absorbed: set[str] = set()
        self.round = 0
        self.ripple: deque[int] = deque()        # paragraphs waiting to absorb a change made before them
        self.busy = False
        # True once the first round has ever committed. Generating into a still-empty canvas (nobody
        # has submitted yet) makes the model write a planning outline instead of prose, so the worker
        # loop waits for a real submission before running the first round.
        self.started = False
        self.last_round = time.time()
        self._stop = threading.Event()

        # Companion illustrations -- a best-effort side channel, never the round loop's critical path.
        self.illustrator = Illustrator()
        self.images: dict[int, dict] = {}          # block -> {"round": n, "data": data-uri}, client-facing
        self._image_bytes: dict[int, bytes] = {}    # block -> raw bytes, fed back for i2i continuity; never sent
        self._image_busy: set[int] = set()          # blocks with a call currently in flight
        self._image_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="loom-image")

    # ---- public API used by the HTTP handler ----

    def contributor_for(self, name: str, cid: str | None = None) -> Contributor:
        """Contributors are identified by an opaque id the phone keeps in localStorage. The colour is
        assigned once and never changes, because it is how the room recognises its own words."""
        with self.lock:
            if cid and cid in self.contributors:
                c = self.contributors[cid]
                if name and name != c.name:
                    c.name = name[:24]
                return c
            c = Contributor(id=cid or uuid.uuid4().hex[:12],
                            name=(name or "anon")[:24],
                            colour=PALETTE[len(self.contributors) % len(PALETTE)])
            self.contributors[c.id] = c
            return c

    def submit(self, text: str, contributor: Contributor, target_id: int | None = None,
               after: int | None = None) -> dict:
        text = re.sub(r"\s+", " ", text).strip()[:MAX_PHRASE]
        if not text:
            raise ValueError("empty phrase")
        target_applied: bool | None = None
        target_reason: str | None = None
        with self.lock:
            if len(self.pending) >= MAX_PENDING:
                raise RuntimeError("the loom is full — try again in a moment")
            mine = sum(1 for p in self.pending if p.contributor == contributor.id)
            if mine >= MAX_PER_PERSON:
                raise RuntimeError(f"you already have {mine} phrases waiting")
            if target_id is not None:
                idx = self._pid_index.get(target_id)
                if idx is None:
                    target_applied, target_reason = False, "that paragraph no longer exists"
                    after = None
                else:
                    target_applied = True
                    if after is not None:
                        n_sents = len(split_sentences(self.paragraphs[idx]))
                        if not 0 <= after < n_sents:
                            after = None   # out of range -- fall back to auto-placement
            else:
                after = None   # an anchor is meaningless without knowing whose sentences it counts
            self.pending.append(Pending(text=text, contributor=contributor.id, at=time.time(),
                                        target_id=target_id if target_applied else None,
                                        after=after if target_applied else None))
            depth = len(self.pending)
        self.bus.publish({"type": "queue", "depth": depth, "pending": self.pending_json()})
        out = {"queued": True, "depth": depth}
        if target_applied is not None:
            out["target_applied"] = target_applied
            if target_reason is not None:
                out["target_reason"] = target_reason
        return out

    def pending_json(self) -> list[dict]:
        with self.lock:
            return [p.as_json() for p in self.pending]

    def state(self) -> dict:
        with self.lock:
            return {
                "paragraphs": list(self.paragraphs),
                "paragraph_ids": list(self.paragraph_ids),
                "live": self.live,
                "round": self.round,
                "busy": self.busy,
                "pending": [p.as_json() for p in self.pending],
                "contributors": {k: v.as_json() for k, v in self.contributors.items()},
                "max_per_round": MAX_PER_ROUND,
                "images": dict(self.images),
            }

    # ---- the round loop ----

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._worker, name="loom-rounds", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()
        self._image_pool.shutdown(wait=False)

    def _worker(self) -> None:
        while not self._stop.is_set():
            batch, block = self._take_batch()
            try:
                if batch:
                    if not self.started:
                        self._bootstrap(batch)
                    else:
                        self._run_round(batch, kind="weave", block=block)
                elif self.ripple:
                    with self.lock:
                        block = self.ripple.popleft()
                    self._run_round([], kind="ripple", block=block)
                else:
                    time.sleep(0.25)
            except Exception as e:                     # a bad round must never kill the loom
                self.bus.publish({"type": "error", "message": str(e)})
                self.last_round = time.time()
                time.sleep(1.0)
            finally:
                with self.lock:
                    self.busy = False

    def _take_batch(self) -> tuple[list[Pending], int | None]:
        """Drain up to MAX_PER_ROUND phrases that all want the same paragraph. Mixing targets in one
        canvas is not possible — a canvas is one paragraph — so the rest keep their place in the queue.
        Returns the batch alongside the paragraph index it resolved to, so the worker can hand that
        straight to `_run_round` as `block` instead of it silently defaulting back to live."""
        with self.lock:
            if not self.pending:
                return [], None

            def resolve(p: Pending) -> int:
                if p.target_id is None:
                    return self.live
                return self._pid_index.get(p.target_id, self.live)

            head = self.pending[0]
            want = resolve(head)
            batch, keep = [], deque()
            for p in self.pending:
                if resolve(p) == want and len(batch) < MAX_PER_ROUND:
                    batch.append(p)
                else:
                    keep.append(p)
            self.pending = keep
            self.busy = True
            return batch, want

    def _requeue(self, batch: list[Pending]) -> None:
        """Return an unconsumed batch to the head of the queue, keeping its original order."""
        if not batch:
            return
        with self.lock:
            for p in reversed(batch):
                self.pending.appendleft(p)
            depth = len(self.pending)
        self.bus.publish({"type": "queue", "depth": depth, "pending": self.pending_json()})

    def _context(self, block: int, budget: int = CONTEXT_PARAS) -> str:
        """The paragraphs before this one, as prompt. Capped: per-step cost grows with prompt length
        (docs/backend.md), and the paragraph being rewoven is in the canvas, not the prompt."""
        with self.lock:
            prior = self.paragraphs[max(0, block - budget):block]
        return "\n\n".join(p.strip() for p in prior if p.strip())

    def _bootstrap(self, batch: list[Pending]) -> None:
        """The very first round. `plan_weave` genuinely supports an empty paragraph (it's just
        submissions floating in free space), but in practice that free space IS almost the entire
        256-position canvas -- there's no pinned prose yet to deny the model room to plan in, which is
        exactly the failure weave.py's docstring warns about: DiffusionGemma writes a planning outline
        instead of story, tidy() strips it, and the round rejects as an empty canvas. Every retry hits
        the same wall (confirmed: 34 straight rejections in testing).

        A plain chat completion doesn't dodge the planning -- tested live, the model still opens with a
        bulleted outline even here, given only a short phrase and no established narrative to continue.
        But the order is the mirror image of a normal round: the outline comes FIRST and clean prose
        comes LAST once the model talks itself into a scene, so `tidy()` (built to cut at the first bad
        line and keep the prefix) is exactly backwards for this -- `_bootstrap_prose` keeps the suffix
        instead. And rather than hope the model repeats a submission verbatim inside that prose (tested:
        it paraphrases), the phrase is seeded into the paragraph directly, so inclusion is guaranteed by
        construction, not by the model's cooperation."""
        with self.lock:
            self.busy = True
            self.round += 1
            n = self.round

        phrases = [p.text for p in batch]
        seed_text = " ".join(phrases)
        seed = 1000 + n
        continuation = ""
        # One retry on a different seed: ~20% of opens come back with no sentence-ending punctuation
        # at all (the model spends its budget in the thought channel and gets cut off mid-clause), and
        # this round only runs once per story -- worth the extra ~20s to not open on the bare phrase.
        for attempt_seed in (seed, seed + 10_000):
            try:
                raw = self._chat(f"{seed_text}\n\nContinue the story.", attempt_seed,
                                 max_tokens=BOOTSTRAP_MAX_TOKENS, eb=BOOTSTRAP_EB)
            except Exception:
                self._requeue(batch)
                raise
            continuation = _bootstrap_prose(raw)
            if continuation:
                sents = split_sentences(continuation)
                continuation = "".join(sents[:BOOTSTRAP_SENTENCES]).strip()
                break
        after = f"{seed_text} {continuation}".strip() if continuation else seed_text

        if not after.strip():
            self._requeue(batch)
            self.bus.publish({"type": "round_rejected", "round": n, "block": 0,
                              "reason": "the opening didn't land"})
            self.last_round = time.time()
            return

        spans = [{"text": p.text, "id": p.contributor} for p in batch if p.text in after]
        self.bus.publish({"type": "round_start", "round": n, "kind": "bootstrap", "block": 0,
                          "seed": seed, "submissions": [
                              {"text": p.text, "contributor": p.contributor,
                               "colour": self.contributors[p.contributor].colour
                                         if p.contributor in self.contributors else None}
                              for p in batch]})

        with self.lock:
            self.paragraphs[0] = after
            self.started = True
            for p in batch:
                if p.text in after:
                    self.absorbed.add(p.text)
            self.history.append(Revision(n=n, at=time.time(), block=0, before="", after=after,
                                         seed=seed, kind="bootstrap", spans=spans,
                                         submissions=[{"text": p.text, "contributor": p.contributor}
                                                      for p in batch]))

        self.bus.publish({"type": "commit", "round": n, "block": 0, "text": after,
                          "spans": spans, "kind": "bootstrap"})
        self._maybe_illustrate(0, n, after)
        self._maybe_split()
        self.last_round = time.time()
        self.bus.publish({"type": "round_end", "round": n, "state": self.state()})

    def _run_round(self, batch: list[Pending], kind: str, block: int | None = None) -> None:
        with self.lock:
            self.busy = True
            self.round += 1
            n = self.round
            target = self.live if block is None else min(block, len(self.paragraphs) - 1)
            retro = kind == "weave" and target != self.live
            before = self.paragraphs[target]
            people = {c.id: c for c in self.contributors.values()}

        subs = [Submission(text=p.text, id=p.contributor, after=p.after) for p in batch]
        protect = tuple(t for t in self.absorbed if t in before)  # nobody's phrase gets deleted
        # A ripple round has nothing new to weave, so it would reproduce the paragraph verbatim -- give
        # it a sentence to rewrite instead. Chosen from the round number rather than at random: the
        # revision log has to replay exactly for the time-lapse at the end.
        dissolve: tuple[int, ...] = ()
        if not subs:
            n_sents = len(split_sentences(before))
            if n_sents > 1:
                dissolve = (n % n_sents,)
        plan = plan_weave(before, subs, self.tok, Policy(), protect=protect, dissolve=dissolve)
        seed = 1000 + n

        self.bus.publish({
            "type": "round_start", "round": n, "kind": kind, "block": target, "retro": retro,
            "seed": seed, "used": plan.used, "canvas": plan.canvas, "notes": plan.notes,
            "submissions": [{"text": p.text, "contributor": p.contributor,
                             "colour": people[p.contributor].colour if p.contributor in people else None}
                            for p in batch],
            "pins": plan.to_pins(),
        })

        try:
            spans, raw = self._generate(plan.to_pins(), seed, self._context(target))
        except Exception:
            # The model went away mid-round. The phrases were already taken off the queue, so put them
            # back before the error propagates -- losing someone's words to a backend restart is the one
            # thing the room will notice and the one thing they cannot re-send, because they walked away.
            self._requeue(batch)
            raise
        after = tidy(raw)

        reason = self._accept(after, plan, target, before)
        if reason:
            # Keep what the room already liked, and put the phrases back at the front of the queue: the
            # next round runs in about six seconds, so a rejected round costs almost nothing.
            self._requeue(batch)
            self.bus.publish({"type": "round_rejected", "round": n, "block": target, "reason": reason})
            self.last_round = time.time()
            return

        with self.lock:
            self.paragraphs[target] = after
            self.started = True
            for p in batch:
                if p.text in after:
                    self.absorbed.add(p.text)
            rev = Revision(n=n, at=time.time(), block=target, before=before, after=after, seed=seed,
                           kind=kind, retro=retro, spans=spans,
                           submissions=[{"text": p.text, "contributor": p.contributor} for p in batch])
            self.history.append(rev)
            # A change to an earlier paragraph has to propagate: every paragraph after it re-denoises in
            # turn, which is the cascade the audience sees travel down the screen.
            if kind == "weave" and target < len(self.paragraphs) - 1:
                for i in range(target + 1, len(self.paragraphs)):
                    if i not in self.ripple:
                        self.ripple.append(i)

        self.bus.publish({"type": "commit", "round": n, "block": target, "text": after,
                          "spans": spans, "kind": kind, "retro": retro})
        self._maybe_illustrate(target, n, after)
        self._maybe_split()
        self.last_round = time.time()
        self.bus.publish({"type": "round_end", "round": n, "state": self.state()})

    def _accept(self, text: str, plan, target: int, before: str) -> str | None:
        """Reason to throw this round away, or None to commit it."""
        story = [p.text.strip() for p in plan.pins if p.role == "story" and p.text.strip()]
        if story:
            # A canvas that trimmed early, or a model that wandered, can drop pinned spans. Committing
            # that would silently delete the room's story.
            kept = sum(1 for s in story if s in text)
            if kept / len(story) < ACCEPT_RATIO:
                return f"only {kept}/{len(story)} pinned spans survived"
        elif not text.strip():
            return "empty canvas"
        run = self._copied_run(text, target, before)
        if run:
            return f"copied a passage: {run[:60]!r}"
        return None

    def _copied_run(self, text: str, target: int, before: str) -> str | None:
        """A verbatim run this round newly duplicated — either lifted out of a neighbouring paragraph or
        repeated twice inside this one. Given a wide gap the model will fill it by restating a sentence
        it can already see, in the prompt or pinned on the canvas beside it.

        Only damage THIS round caused counts, so both tests are measured against `before` — otherwise a
        duplicate that once slipped through would reject every future round forever and freeze the
        paragraph at the very moment it most needs rewriting. For the repeat test that means comparing
        COUNTS, not membership: a sentence the paragraph already contained once, now appearing twice, is
        new damage even though the run itself is not new."""
        with self.lock:
            others = [p for i, p in enumerate(self.paragraphs) if i != target and p.strip()]
        words = text.split()
        for i in range(max(0, len(words) - DUP_RUN + 1)):
            run = " ".join(words[i:i + DUP_RUN])
            if text.count(run) > max(1, before.count(run)):
                return run                                        # newly repeated inside this paragraph
            if run not in before and any(run in o for o in others):
                return run                                        # newly lifted from a neighbour
        return None

    def _maybe_illustrate(self, block: int, round_n: int, text: str) -> None:
        """Fire a background illustration call for this round's committed text. Skipped, not queued, if
        a call for this block is already in flight -- that call's result is about to be superseded by
        this round's text anyway, so queuing another request would just spend a second API call on stale
        text."""
        if not self.illustrator.enabled:
            return
        with self.lock:
            if block in self._image_busy:
                return
            self._image_busy.add(block)
        self._image_pool.submit(self._illustrate_worker, block, round_n, text)

    def _illustrate_worker(self, block: int, round_n: int, text: str) -> None:
        try:
            with self.lock:
                previous = self._image_bytes.get(block)
            result = self.illustrator.illustrate(text, previous)
            if result is None:
                return
            img_bytes, mime = result
            data_uri = f"data:{mime};base64,{base64.b64encode(img_bytes).decode()}"
            with self.lock:
                # a newer round may have already landed an image for this block while we were waiting on
                # the network -- drop this one rather than overwrite something fresher with something stale.
                if self.images.get(block, {}).get("round", -1) >= round_n:
                    return
                self._image_bytes[block] = img_bytes
                self.images[block] = {"round": round_n, "data": data_uri}
            self.bus.publish({"type": "image", "round": round_n, "block": block, "data": data_uri})
        finally:
            with self.lock:
                self._image_busy.discard(block)

    def _maybe_split(self) -> None:
        """Trim the live paragraph back to what the canvas can weave, and settle the overflow behind it.

        The overflow joins the PREVIOUS paragraph while that one has room, rather than becoming a new
        paragraph every round: a round only adds forty or so tokens, and one paragraph per forty tokens
        would shred the story into fragments. A new paragraph starts only once the one behind is full."""
        with self.lock:
            text = self.paragraphs[self.live]
        sents = split_sentences(text)
        if len(sents) < 2:
            return
        counts = self.tok.counts(sents)
        if sum(counts) <= SPLIT_AT:
            return

        # Keep the longest tail that still fits the weave budget; everything before it has settled.
        keep, total = 0, 0
        for c in reversed(counts):
            if total + c > SPLIT_AT:
                break
            total, keep = total + c, keep + 1
        keep = max(1, keep)
        if keep >= len(sents):
            return
        head = "".join(sents[:-keep]).rstrip()
        tail = "".join(sents[-keep:]).strip()
        head_n = sum(counts[:-keep])

        with self.lock:
            self.paragraphs[self.live] = tail
            prev = self.live - 1
            if prev >= 0 and self.tok.count(self.paragraphs[prev]) + head_n <= PARA_MAX:
                # The overflow joins prev's existing identity -- it's the same paragraph, just longer.
                self.paragraphs[prev] = f"{self.paragraphs[prev].rstrip()} {head}".strip()
            else:
                # The overflow becomes a new paragraph with a fresh id, inserted just ahead of the live
                # one -- which keeps its own id at its new, shifted-by-one index: it's still the same
                # paragraph that was live a moment ago, just shorter.
                self.paragraphs.insert(self.live, head)
                self.paragraph_ids.insert(self.live, self._next_pid)
                self._next_pid += 1
                self.live += 1
                self._pid_index = {pid: i for i, pid in enumerate(self.paragraph_ids)}
            live, n = self.live, len(self.paragraphs)
        self.bus.publish({"type": "split", "live": live, "paragraphs": n, "state": self.state()})

    # ---- the model ----

    def _chat(self, prompt: str, seed: int, max_tokens: int = 256, eb: dict = EB) -> str:
        """A plain, non-diffusion-canvas completion -- used only by `_bootstrap`. Needs `EB` tuning same
        as `_generate` -- without it this call runs at the backend's hotter defaults, and it's the one
        round with no pinned prose and no gap cap to keep it from rambling or looping."""
        body = {"messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                "max_tokens": max_tokens, "seed": seed, **eb}
        req = urllib.request.Request(f"{self.api}/v1/chat/completions",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=300) as r:
            resp = json.load(r)
        return resp["choices"][0]["message"]["content"]

    def _generate(self, pins: list[dict], seed: int, context: str) -> tuple[list[dict], str]:
        user = (f"{context}\n\nContinue the story." if context else "Continue the story.")
        body = {
            "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
            "max_tokens": 256, "seed": seed, "pins": pins, **EB,
        }
        req = urllib.request.Request(f"{self.api}/v1/diffusion/stream",
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        spans: list[dict] = []
        raw = ""
        with urllib.request.urlopen(req, timeout=300) as r:
            for line in r:
                line = line.decode("utf-8", "replace").strip()
                if not line.startswith("data: "):
                    continue
                payload = line[6:]
                if payload == "[DONE]":
                    break
                try:
                    ev = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                kind = ev.get("type")
                if kind == "frame":
                    # The reason anyone is watching. Droppable: a projector that falls behind should
                    # skip frames, never delay the commit.
                    self.bus.publish(ev, droppable=True)
                elif kind == "pins":
                    spans = ev.get("spans", [])
                    self.bus.publish(ev)
                elif kind == "commit":
                    raw = ev.get("raw") or ev.get("answer") or ""
                elif kind == "error":
                    raise RuntimeError(ev.get("message", "backend error"))
        return spans, raw


# --------------------------------------------------------------------------------------------------
# http
# --------------------------------------------------------------------------------------------------

LOOM: Loom | None = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "loom/1.0"

    def log_message(self, fmt, *a):
        sys.stderr.write("[loom] %s - %s\n" % (self.address_string(), fmt % a))

    def _json(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int, msg: str) -> None:
        self._json(code, {"error": msg})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/") or "/"
        if path in ("/health", "/healthz"):
            return self._json(200, {"status": "ok", "round": LOOM.round, "api": LOOM.api})
        if path == "/state":
            return self._json(200, LOOM.state())
        if path == "/history":
            return self._json(200, {"revisions": [r.as_json() for r in LOOM.history]})
        if path == "/stream":
            return self._stream()
        return self._err(404, f"unknown path {self.path}")

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        if path != "/submit":
            return self._err(404, f"unknown path {self.path}")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._err(400, f"bad JSON: {e}")
        text = req.get("text")
        if not isinstance(text, str) or not text.strip():
            return self._err(400, "'text' must be a non-empty string")
        who = LOOM.contributor_for(str(req.get("name") or ""), req.get("id"))
        target_id = req.get("target_id")
        target_id = int(target_id) if isinstance(target_id, (int, float)) else None
        after = req.get("after")
        after = int(after) if isinstance(after, (int, float)) else None
        try:
            out = LOOM.submit(text, who, target_id, after)
        except ValueError as e:
            return self._err(400, str(e))
        except RuntimeError as e:
            return self._err(429, str(e))
        return self._json(200, {**out, "contributor": who.as_json()})

    def _stream(self):
        """SSE for the projector. Opens with the full state so a client that connects mid-round renders
        something immediately rather than waiting for the next commit."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        q = LOOM.bus.subscribe()
        try:
            self._sse({"type": "state", **LOOM.state()})
            while True:
                try:
                    self._sse(q.get(timeout=15))
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")   # keep proxies and phones from hanging up
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            LOOM.bus.unsubscribe(q)

    def _sse(self, obj: dict) -> None:
        self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
        self.wfile.flush()


def tailscale_ip() -> str:
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    except Exception:
        return ""


def main() -> None:
    global LOOM
    host = os.environ.get("LOOM_HOST") or tailscale_ip()
    if not host:
        sys.exit("ERROR: no Tailscale IPv4 found. Is tailscaled up? (tailscale status)")
    if host == "0.0.0.0":
        sys.exit("ERROR: refusing to bind 0.0.0.0 — set LOOM_HOST to a specific address")

    LOOM = Loom()
    if not LOOM.illustrator.enabled:
        print("[loom] no Gemini keys found (loom/.env: GEMINI_KEY_1/GEMINI_KEY_2) — "
              "running text-only, no illustrations")
    try:
        with urllib.request.urlopen(f"{LOOM.api}/health", timeout=5) as r:
            json.load(r)
    except Exception as e:
        sys.exit(f"ERROR: DiffusionGemma is not reachable at {LOOM.api} ({e}). Start ./run-server.sh")

    LOOM.start()
    srv = ThreadingHTTPServer((host, PORT), Handler)
    srv.daemon_threads = True
    print(f"[loom] the loom is running on http://{host}:{PORT}")
    print(f"[loom]   submit  ->  POST http://{host}:{PORT}/submit")
    print(f"[loom]   project ->  GET  http://{host}:{PORT}/stream")
    print(f"[loom]   model   ->  {LOOM.api}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[loom] shutting down")
    finally:
        LOOM.stop()
        srv.server_close()


if __name__ == "__main__":
    main()
