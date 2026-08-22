"""Weave planner: turn "the story so far" plus a handful of audience phrases into a `pins` array.

Generating into an empty canvas makes DiffusionGemma write a planning outline, and no prompt stops it
(docs/progress.md). Pinning the existing prose back into the canvas with gaps leaves no room for one:
the model can only write in the gaps, so it writes connective tissue.

The whole job here is deciding where those gaps go. Pins are laid at absolute token positions, so the
planner needs exact token counts -- it gets them from the backend's own vocab via /v1/tokenize rather
than carrying a second tokenizer that could disagree with the model.

Four policy knobs matter. Each one exists because of a seam observed in real output, and each was
measured against the alternative -- run loom/seamtest.py to reproduce:

  pin_terminal_punct=True   Keep the sentence-final "." inside the pin. Withholding it was tried, on the
                            theory that the model wanted to continue a clause through the punctuation --
                            it is measurably worse (loom/seamtest.py case B). Without a pinned period
                            the model often fails to supply one and words fuse: "dry kindlingThe
                            suitcase". A lowercase word after a period is a milder defect than that.

  open_last                 The final kept sentence is pinned without its period, because that is where
                            the story continues and the model wants to extend it. Doing this to EVERY
                            sentence fuses words together (case B); doing it only at the growth point
                            fixes the one seam that survives every other policy. It is withdrawn again
                            when a LARGE gap follows: over a short gap the model continues the sentence,
                            but given eighty free positions it starts a fresh one and never supplies the
                            missing period -- "three short beats, a pause, and three more again The
                            keeper froze."

  max_gap                   No single gap may exceed this. A gap of 100+ positions is not an invitation
                            to write well, it is where the model loops: "a low, guttural sound sound...
                            a grinding grinding grinding against the shore."

  max_tail                  Cap on free positions left at the end. Leftover tail space is not harmless:
                            given 150 spare positions after the weave is done, the model fills them with
                            planning notes or meta-commentary about the prompt. Excess goes into the
                            working gaps instead.

  lead_gap / trail_gap      Positions left free either side of a submission, so the model can build a
                            clause around the phrase instead of butting prose against it.

Nothing here talks to the model. plan_weave() is pure layout; run it, inspect it, then post the pins.
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field

CANVAS = 256          # positions per diffusion block; fixed by the model


# --------------------------------------------------------------------------------------------------
# tokenization
# --------------------------------------------------------------------------------------------------

class Tokenizer:
    """Token counts from the backend's vocab, memoized. Every span the planner considers gets measured,
    and the same story sentences come back round after round, so the cache earns its keep quickly."""

    def __init__(self, api="http://100.70.13.60:8080", timeout=30):
        self.api, self.timeout = api.rstrip("/"), timeout
        self._cache: dict[str, list[str]] = {"": []}

    def pieces(self, *texts) -> list[list[str]]:
        missing = [t for t in dict.fromkeys(texts) if t not in self._cache]
        if missing:
            body = json.dumps({"texts": missing}).encode()
            req = urllib.request.Request(f"{self.api}/v1/tokenize", data=body,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                got = json.load(r)["tokens"]
            self._cache.update(zip(missing, got))
        return [self._cache[t] for t in texts]

    def count(self, text) -> int:
        return len(self.pieces(text)[0])

    def counts(self, texts) -> list[int]:
        return [len(p) for p in self.pieces(*texts)]


# --------------------------------------------------------------------------------------------------
# output hygiene
# --------------------------------------------------------------------------------------------------

# The two failure modes a woven canvas still shows occasionally: the model spends leftover positions on
# a planning outline, or on commentary about the prompt. seamtest.py scores against these; the server
# trims at them, because either one on a projector is worse than a slightly shorter paragraph.
OUTLINE = re.compile(r"^\s*[*\-]\s{2,}|\*\s+\*", re.M)
META = re.compile(r"the user|Constraint:|Self-Correction|the prompt|\bWait\b|didn't (?:actually )?provide")
CHANNEL = re.compile(r"<\|channel>(?:thought)?\s*|<channel\|>")
# The loom's own words to the model, echoed back into the canvas. These end in sentence punctuation,
# so every "keep whole sentences" trim waves them through -- they have to be removed by name, and
# removed rather than cut at, because the model can echo them ahead of perfectly good prose.
# Mirrors SYSTEM and the user turn in loom/server.py; keep the two in sync.
ECHO = re.compile(r"(?i)(?:you are a )?prose continuation engine\.?|\bprose only\b\.?"
                  r"|\bcontinue the story\b\.?")


def tidy(text: str) -> str:
    """Strip channel markers and echoed prompt text, and cut at the first line that stops being story."""
    text = CHANNEL.sub("", text)
    if ECHO.search(text):
        text = re.sub(r" {2,}", " ", ECHO.sub("", text))
    cut = len(text)
    for rx in (OUTLINE, META):
        m = rx.search(text)
        if not m:
            continue
        # Cut at the start of the offending line, not mid-sentence -- the sentence before it is usually
        # still good prose and worth keeping.
        nl = text.rfind("\n", 0, m.start())
        cut = min(cut, m.start() if nl < 0 else nl)
    return text[:cut].rstrip()


# --------------------------------------------------------------------------------------------------
# sentence splitting
# --------------------------------------------------------------------------------------------------

# Sentence-final punctuation, any closing quotes/brackets after it, and the whitespace that follows --
# all of which belong to the sentence being ended, so that "".join(split_sentences(s)) == s.
_END = re.compile(r'[.!?]+["”\'\)\]]*\s+')
_TAIL = re.compile(r'([.!?]+["”\'\)\]]*\s*)$')


def split_sentences(text: str) -> list[str]:
    """Cut on sentence-final punctuation, keeping the punctuation and trailing space with the sentence
    it closes. Deliberately dumb: an abbreviation splits wrongly, and the cost of that is one slightly
    odd pin boundary, not a broken weave."""
    out, start = [], 0
    for m in _END.finditer(text):
        out.append(text[start:m.end()])
        start = m.end()
    if text[start:].strip():
        out.append(text[start:])
    return out


def split_tail(sentence: str) -> tuple[str, str]:
    """(body, terminal punctuation + trailing space). Body is what gets pinned when the planner is
    leaving the sentence open-ended."""
    m = _TAIL.search(sentence)
    return (sentence[:m.start()], m.group(1)) if m else (sentence, "")


# --------------------------------------------------------------------------------------------------
# plan
# --------------------------------------------------------------------------------------------------

@dataclass
class Submission:
    """One phrase from the room. `after` is the sentence index to insert behind; None spreads
    submissions evenly across the paragraph."""
    text: str
    id: str
    after: int | None = None


@dataclass
class Policy:
    canvas: int = CANVAS
    pin_terminal_punct: bool = True   # keep the closing "." inside the pin; see the module docstring
    open_last: bool = True            # ...except on the final kept sentence, which is where the story
                                      # grows. Pinning its period leaves the model writing ". with a
                                      # metallic click." -- it wanted to extend the sentence and the
                                      # punctuation was in the way. Interior sentences keep their periods
                                      # because the next pinned span supplies the capital after them.
    min_gap: int = 4                  # below this the model has no room to write anything
    max_gap: int = 60                 # above this the model starts repeating itself; see the docstring
    open_last_max_gap: int = 20       # withdraw open_last when the gap after it is bigger than this
    punct_gap: int = 3                # positions left where a terminal "." was withheld
    dissolve_slack: int = 6           # extra room over the original length of a dissolved sentence
    lead_gap: int = 8                 # free positions before a submission
    trail_gap: int = 10               # free positions after a submission
    dissolve_after: bool = True       # rewrite the sentence following each submission
    dissolve_before: bool = False     # ...and the one preceding it
    max_tail: int = 24                # free positions allowed at the end, where the story grows. Anything
                                      # beyond this goes into the working gaps: a large idle tail is where
                                      # the model starts writing notes to itself instead of prose.


@dataclass
class Element:
    """One item in the left-to-right layout: either pinned text or a run of free positions."""
    kind: str                    # "pin" | "gap"
    text: str = ""
    id: str = ""
    role: str = ""               # "story" | "submission" | "punct" | "rewrite"
    target: int = 0              # requested size (gaps); natural token length (pins)
    size: int = 0                # size after budget fitting
    pos: int = -1
    withheld: str = ""           # terminal punctuation held back from this pin, restorable post-fit


@dataclass
class WeavePlan:
    elements: list[Element]
    canvas: int
    notes: list[str] = field(default_factory=list)

    @property
    def pins(self) -> list[Element]:
        return [e for e in self.elements if e.kind == "pin" and e.size > 0]

    @property
    def gaps(self) -> list[Element]:
        return [e for e in self.elements if e.kind == "gap" and e.size > 0]

    @property
    def used(self) -> int:
        return sum(e.size for e in self.elements)

    @property
    def free(self) -> int:
        return sum(e.size for e in self.gaps) + (self.canvas - self.used)

    def to_pins(self) -> list[dict]:
        """The `pins` array to POST. Ordered by position, which is also overwrite order."""
        return [{"pos": e.pos, "text": e.text, "id": e.id} for e in self.pins]

    def render(self) -> str:
        """One line per element -- what a human reads to see whether the layout makes sense."""
        rows = []
        for e in self.elements:
            if e.size <= 0:
                continue
            if e.kind == "gap":
                rows.append(f"  {e.pos:>3}  ....  {e.size:>2} free   ({e.role})")
            else:
                rows.append(f"  {e.pos:>3}  PIN   {e.size:>2} tok  [{e.id}] {e.text[:58]!r}")
        tail = self.canvas - self.used
        if tail > 0:
            rows.append(f"  {self.used:>3}  ....  {tail:>2} free   (tail)")
        return "\n".join(rows)


def _merge_gaps(elements: list[Element]) -> list[Element]:
    """Adjacent gaps are one gap. Two 4-position gaps back to back are not the same offer to the model
    as one 8-position gap -- only the second is enough room for a clause."""
    out: list[Element] = []
    for e in elements:
        if e.kind == "gap" and out and out[-1].kind == "gap":
            out[-1].target += e.target
            if e.role not in out[-1].role:
                out[-1].role = f"{out[-1].role}+{e.role}"
            continue
        out.append(e)
    return out


def _open_seams(elements: list[Element], tok: Tokenizer) -> None:
    """Put the whitespace at every pin/gap boundary on the side the tokenizer wants it.

    Gemma carries a word's leading space inside the word token: " It" is one token, "It" is a different
    and much rarer one. That makes both edges of a gap fragile, in opposite directions:

      pin then gap   A pin of "...listening. " spends a standalone " " token, so the gap can only open
                     with the spaceless variant -- and the model drops the word rather than use it:
                     "suspect the sea of listening.  wasn't just the crashing". Strip the pin.

      gap then pin   A pin of "Behind him..." has no leading space, so the model must CLOSE its gap on a
                     trailing-space token, which is just as unnatural: "the soles of the
                     floorboards.Behind him". Give the pin the space.
    """
    prev = None
    for e, nxt in zip(elements, elements[1:] + [None]):
        if e.kind == "pin":
            text = e.text
            if (nxt is None or nxt.kind == "gap") and text != text.rstrip():
                text = text.rstrip()
            if prev is not None and prev.kind == "gap" and text[:1] not in (" ", ""):
                text = " " + text
            if text != e.text:
                e.text, e.target = text, tok.count(text)
        prev = e


def _fit(elements: list[Element], policy: Policy) -> list[str]:
    """Resolve `target` into `size` inside the canvas budget, and assign positions. Returns notes on
    anything that had to give way, because silently dropping half the story is not an outcome the
    caller should have to discover by reading the output."""
    notes: list[str] = []
    budget = policy.canvas

    for e in elements:
        e.size = e.target

    pinned = sum(e.size for e in elements if e.kind == "pin")
    if pinned > budget:
        # Even with every gap closed the pins do not fit. Drop whole elements off the tail: a truncated
        # paragraph reweaves fine next round, a clipped pin is a word cut in half on the projector.
        while elements and sum(e.size for e in elements if e.kind == "pin") > budget:
            dropped = elements.pop()
            if dropped.kind == "pin":
                notes.append(f"dropped pin [{dropped.id}] {dropped.text[:40]!r} — canvas full")
        pinned = sum(e.size for e in elements if e.kind == "pin")

    gaps = [e for e in elements if e.kind == "gap"]
    want = sum(e.target for e in gaps)
    room = budget - pinned

    if want > room and gaps:
        # Shrink gaps proportionally toward min_gap rather than starving the last few: an even squeeze
        # degrades every junction a little, dropping gaps degrades a few junctions completely.
        floor = sum(min(policy.min_gap, e.target) for e in gaps)
        if room <= floor:
            for e in gaps:
                e.size = min(policy.min_gap, e.target)
            notes.append(f"all gaps at floor ({policy.min_gap}) — canvas is {want - room} over")
        else:
            scale = (room - floor) / (want - floor)
            for e in gaps:
                base = min(policy.min_gap, e.target)
                e.size = base + int((e.target - base) * scale)
            notes.append(f"gaps scaled to {scale:.0%} — {want} requested, {room} available")
    elif want < room - policy.max_tail and gaps:
        # Spend the surplus down to max_tail. It goes to the gaps doing real work, never to a punctuation
        # slot -- that only needs room for a "." and a spare 20 positions there becomes an unplanned
        # sentence. Roles are matched by substring because merging concatenates them ("punct+lead").
        working = [e for e in gaps if any(r in e.role for r in ("rewrite", "lead", "trail"))] or gaps
        extra, given, i = room - policy.max_tail - want, 0, 0
        while given < extra:
            open_gaps = [e for e in working if e.size < policy.max_gap]
            if not open_gaps:
                break                               # everything is at max_gap; the rest stays as tail
            open_gaps[i % len(open_gaps)].size += 1
            given, i = given + 1, i + 1
        notes.append(f"spread {given} of {extra} surplus positions into {len(working)} gaps"
                     + ("" if given == extra else f"; {extra - given} left as tail (max_gap)"))

    cursor = 0
    for e in elements:
        e.pos = cursor
        cursor += e.size
    return notes


def _reclose(elements: list[Element], policy: Policy, tok: Tokenizer) -> bool:
    """Put back a withheld terminal period when the model will not use the opening.

    Withholding it only pays off if the model goes on to extend that sentence. It does when the sentence
    is the last pinned thing on the canvas -- the free space after it is growth, and it grows. It does
    not when more pinned text follows across a wide gap: there the model starts a fresh sentence, never
    supplies the missing period, and the spans fuse ("three more again The keeper froze")."""
    idx = max((i for i, e in enumerate(elements) if e.kind == "pin" and e.withheld), default=-1)
    if idx < 0:
        return False
    rest = elements[idx + 1:]
    if not any(x.kind == "pin" for x in rest):
        return False                                  # it is the growth point; leaving it open is right
    if sum(x.size for x in rest) <= policy.open_last_max_gap:
        return False                                  # a short hop; the model will carry the clause over
    e = elements[idx]
    e.text += e.withheld
    e.withheld = ""
    e.target = tok.count(e.text)
    return True


def plan_weave(paragraph: str, submissions: list[Submission], tok: Tokenizer,
               policy: Policy | None = None, protect: tuple[str, ...] = (),
               dissolve: tuple[int, ...] = ()) -> WeavePlan:
    """Lay the paragraph back into the canvas with gaps, and drop the submissions into those gaps.

    `protect` is text that must survive this round: any sentence containing it is kept rather than
    dissolved. Phrases the room has already contributed go here. Without it a later round eventually
    picks the sentence holding someone's phrase as its rewrite target and quietly deletes it, which is
    the one failure the whole exhibit cannot afford.

    `dissolve` names extra sentence indices to rewrite. A round with no submissions has nothing to
    change and reproduces the paragraph verbatim, so the caller passes one here to keep the story alive.

    An empty paragraph is fine -- the result is submissions floating in free space, which is what the
    first round of a story looks like."""
    policy = policy or Policy()
    sents = split_sentences(paragraph)

    # Where each submission goes. Explicit `after` wins; the rest spread evenly, so two phrases arriving
    # in the same round land in different parts of the paragraph instead of on top of each other.
    seams: dict[int, list[Submission]] = {}
    auto = [s for s in submissions if s.after is None]
    for s in submissions:
        if s.after is not None:
            seams.setdefault(max(-1, min(s.after, len(sents) - 1)), []).append(s)
    for i, s in enumerate(auto):
        at = -1 if not sents else int((i + 1) * len(sents) / (len(auto) + 1)) - 1
        seams.setdefault(at, []).append(s)

    # Which sentences get rewritten rather than kept. A submission's neighbours have to move for the
    # phrase to fit; everything else stays, so the story the room already liked survives the round.
    dissolved: set[int] = set(i for i in dissolve if 0 <= i < len(sents))
    for at in seams:
        if policy.dissolve_after and at + 1 < len(sents):
            dissolved.add(at + 1)
        if policy.dissolve_before and at >= 0:
            dissolved.add(at)
    if protect:
        dissolved -= {i for i, s in enumerate(sents) if any(p and p in s for p in protect)}

    measured = tok.counts(sents) if sents else []
    kept = [i for i in range(len(sents)) if i not in dissolved]
    last_kept = kept[-1] if kept else -1
    elements: list[Element] = []

    def emit_submissions(at: int) -> None:
        for s in seams.get(at, []):
            text = s.text if s.text[:1].isspace() else " " + s.text
            elements.append(Element("gap", role="lead", target=policy.lead_gap))
            elements.append(Element("pin", text=text, id=s.id, role="submission",
                                    target=tok.count(text)))
            elements.append(Element("gap", role="trail", target=policy.trail_gap))

    emit_submissions(-1)                       # a submission placed before the first sentence
    for i, sent in enumerate(sents):
        if i in dissolved:
            elements.append(Element("gap", role="rewrite", target=measured[i] + policy.dissolve_slack))
        elif policy.pin_terminal_punct and not (policy.open_last and i == last_kept):
            elements.append(Element("pin", text=sent, id="story", role="story", target=measured[i]))
        else:
            body, tail = split_tail(sent)
            elements.append(Element("pin", text=body, id="story", role="story",
                                    target=tok.count(body), withheld=tail))
            if tail:
                # Withhold the closing punctuation: the model decides whether to stop here or run on.
                elements.append(Element("gap", role="punct",
                                        target=max(policy.punct_gap, tok.count(tail))))
        emit_submissions(i)

    elements = _merge_gaps([e for e in elements if e.kind == "pin" or e.target > 0])
    _open_seams(elements, tok)
    notes = _fit(elements, policy)
    if _reclose(elements, policy, tok):
        # The restored punctuation brings its trailing space back with it, so the seams have to be
        # reopened before refitting -- otherwise the pin ends on a " " token again and the gap after it
        # loses its leading-space word. Refitting is exact: _fit only reads `target`.
        _open_seams(elements, tok)
        notes = _fit(elements, policy) + ["restored the withheld period — too large a gap followed it"]
    return WeavePlan(elements=elements, canvas=policy.canvas, notes=notes)
