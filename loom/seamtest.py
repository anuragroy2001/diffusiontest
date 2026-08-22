"""Score weave policies against the live model.

Seam quality is the one thing about The Loom that can't be reasoned out -- it has to be measured. This
runs the same paragraph and submission through several Policy configurations and reports, for each, the
defects that actually showed up in hand-run output:

    outline      the model reverted to writing a planning outline instead of prose
    lc_after_dot ". the" -- a clause the model wanted to continue, blocked by a pinned period
    fused        "kindlingThe" -- two spans run together with no space, from a withheld period
    bracketed    the model wrapped pinned spans in (parens) or [brackets], treating them as quoted
    meta         commentary about the prompt instead of story ("the user didn't provide...")
    punct_run    ".." / " ," / ",," left where two spans met
    dup_word     "the the" at a junction
    lost_pins    a pinned span missing from the committed text (clipped, or trimmed away)

Lower is better on every one. The text is printed too, because none of these catch prose that is
technically clean and reads like nothing.

    python3 loom/seamtest.py [--api URL] [--seed N]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from weave import META, OUTLINE, Policy, Submission, Tokenizer, plan_weave   # noqa: E402

PARAGRAPH = (
    "Elias knelt on the wet stones, his knees popping like dry kindling. "
    "The suitcase was a deep, bruised leather, its brass latches gleaming as if the salt air had never "
    "touched them. "
    "He reached out a trembling hand, expecting the surface to be slick with brine. "
    "Behind him, the horizon seemed to lean in, a silent, grey witness. "
    "He set it on the scarred table and worked the latches until they gave."
)
SUBMISSION = Submission(" a stolen accordion", "judge-red")

# Keep the prompt minimal. Telling the model to "keep every word already written" made it treat the
# pinned spans as quoted material and wrap them in brackets -- the pins already guarantee preservation,
# so saying so out loud only gives it something to react to.
SYSTEM = "You are a prose continuation engine. Prose only."
USER = "Continue the lighthouse keeper's story."

_LC_DOT = re.compile(r"[.!?]\s+[a-z]")
_FUSED = re.compile(r"[a-z][.!?]?[A-Z]")   # "kindlingThe" and "floorboards.Behind" alike
_PUNCT_RUN = re.compile(r"[.,;:]{2,}|\s+[,;:]|\s\.")
_DUP_WORD = re.compile(r"\b(\w+)\s+\1\b", re.I)



def _bracketed(text: str, plan) -> int:
    """Pinned spans the model fenced off in (parens) or [brackets] instead of writing around."""
    n = 0
    for p in plan.pins:
        body = p.text.strip()
        if not body:
            continue
        for lhs, rhs in (("(", ")"), ("[", "]")):
            if f"{lhs}{body}" in text or f"{body}{rhs}" in text:
                n += 1
                break
    return n


def score(text: str, plan) -> dict:
    lost = [p.text.strip() for p in plan.pins if p.text.strip() and p.text.strip() not in text]
    return {
        "outline": len(OUTLINE.findall(text)),
        "lc_after_dot": len(_LC_DOT.findall(text)),
        "fused": len(_FUSED.findall(text)),
        "bracketed": _bracketed(text, plan),
        "meta": len(META.findall(text)),
        "punct_run": len(_PUNCT_RUN.findall(text)),
        "dup_word": len(_DUP_WORD.findall(text)),
        "lost_pins": len(lost),
        "_lost": lost,
    }


def generate(api: str, pins: list[dict], seed: int, steps: int = 24) -> tuple[str, float]:
    body = {
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": USER}],
        "max_tokens": 256, "eb_max_steps": steps, "seed": seed,
        "eb_t_max": 0.6, "eb_t_min": 0.3, "frame_every": 1000, "pins": pins,
    }
    req = urllib.request.Request(f"{api}/v1/diffusion/stream", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    t0, raw = time.time(), ""
    with urllib.request.urlopen(req, timeout=900) as r:
        for line in r:
            line = line.decode().strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            ev = json.loads(payload)
            if ev["type"] == "commit":
                raw = ev["raw"]
            elif ev["type"] == "error":
                raise SystemExit("backend error: " + ev["message"])
    return raw, time.time() - t0


CASES: list[tuple[str, Policy]] = [
    ("A default",                 Policy()),
    ("B all periods pinned",      Policy(open_last=False)),
    ("C punctuation free",        Policy(pin_terminal_punct=False)),
    ("D wider submission gaps",   Policy(lead_gap=14, trail_gap=18)),
    ("E tight gaps",              Policy(lead_gap=4, trail_gap=4, dissolve_slack=2, punct_gap=2)),
    ("F uncapped tail",           Policy(max_tail=256)),
]


# A round where several phrases arrive at once -- the real case, since the backend is single-threaded and
# the Loom server drains its whole submission queue into one denoise rather than one generation per phone.
CROWD = [
    Submission(" a stolen accordion", "judge-red"),
    Submission(" it was Tuesday on Mars", "judge-blue"),
    Submission(" she never trusted the notary", "judge-green"),
]


def stability(api: str, tok: Tokenizer, n: int) -> None:
    """The default policy across n seeds, and once with a full submission queue. One clean run proves
    nothing -- the denoise is seeded, and a policy that only works at seed 11 is not a policy."""
    keys = ["outline", "lc_after_dot", "fused", "bracketed", "meta", "punct_run", "dup_word", "lost_pins"]
    rows = []
    for i in range(n):
        seed = 11 + i * 7
        plan = plan_weave(PARAGRAPH, [SUBMISSION], tok, Policy())
        text, secs = generate(api, plan.to_pins(), seed)
        rows.append((f"default seed={seed}", score(text, plan), secs, text))
    plan = plan_weave(PARAGRAPH, CROWD, tok, Policy())
    text, secs = generate(api, plan.to_pins(), 11)
    rows.append((f"3 submissions ({plan.used}/{plan.canvas})", score(text, plan), secs, text))

    for label, s, secs, text in rows:
        total = sum(s[k] for k in keys)
        print(f"\n{'=' * 100}\n[{label}]  {secs:.1f}s  total={total}  "
              + "  ".join(f"{k}={s[k]}" for k in keys if s[k]))
        print("\n" + text.strip()[:1400])

    print(f"\n{'=' * 100}\nSTABILITY (lower is better)\n")
    print(f"  {'run':<28}" + "".join(f"{k:>13}" for k in keys) + f"{'total':>8}{'secs':>7}")
    for label, s, secs, _ in rows:
        print(f"  {label:<28}" + "".join(f"{s[k]:>13}" for k in keys)
              + f"{sum(s[k] for k in keys):>8}{secs:>7.1f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://100.70.13.60:8080")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--stability", type=int, metavar="N",
                    help="skip the policy comparison; run the default policy across N seeds")
    args = ap.parse_args()

    tok = Tokenizer(args.api)
    if args.stability:
        return stability(args.api, tok, args.stability)
    results = []
    for label, policy in CASES:
        plan = plan_weave(PARAGRAPH, [SUBMISSION], tok, policy)
        text, secs = generate(args.api, plan.to_pins(), args.seed)
        s = score(text, plan)
        results.append((label, s, secs, plan))
        print(f"\n{'=' * 100}\n[{label}]  {secs:.1f}s  used {plan.used}/{plan.canvas}")
        if plan.notes:
            print("  notes: " + "; ".join(plan.notes))
        print("  " + "  ".join(f"{k}={v}" for k, v in s.items() if not k.startswith("_")))
        if s["_lost"]:
            print("  LOST: " + "; ".join(repr(x[:40]) for x in s["_lost"]))
        print("\n" + text.strip()[:1500])

    print(f"\n{'=' * 100}\nSUMMARY (lower is better)\n")
    keys = ["outline", "lc_after_dot", "fused", "bracketed", "meta", "punct_run", "dup_word", "lost_pins"]
    print(f"  {'case':<28}" + "".join(f"{k:>13}" for k in keys) + f"{'total':>8}{'secs':>7}")
    for label, s, secs, _ in results:
        total = sum(s[k] for k in keys)
        print(f"  {label:<28}" + "".join(f"{s[k]:>13}" for k in keys) + f"{total:>8}{secs:>7.1f}")


if __name__ == "__main__":
    main()
