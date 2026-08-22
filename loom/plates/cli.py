"""Drive a plate chain from prompts, to feel out instruction wording before it goes on the round loop.

    python3 loom/plates/cli.py "A lone knight travels through an ancient forest"

That renders the opening plate and drops you at a prompt. Every line you type is an *edit* of the
current plate -- "he's actually a robot", "the year is 3000" -- which is exactly the shape a Loom round
has. `:view` writes a page that cross-dissolves the chain so the morph can be judged.

Commands: :view :new <prompt> :model lite|flash|pro :style <text> :undo :redo :ls :where :q
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from plates import viewer                                    # noqa: E402
from plates.gemini import MODELS, PlateError, Plates          # noqa: E402

# Appended to every edit. This clause is doing the real work: it is what holds camera, pose and layout
# still between plates, and holding them still is the entire reason a cross-fade reads as a morph rather
# than a cut. Measured -- without it the model re-frames the shot and the chain stops being a chain.
PRESERVE = (" Keep the same character, pose, camera angle, composition and lighting. "
            "Change only what this instruction asks for.")

# Appended to the opening prompt only. A consistent look makes every later edit cheaper to keep on model.
STYLE = "Cinematic painterly digital art, muted cold palette, dramatic rim light, 16:9."

OUT = Path(__file__).resolve().parent / "out"


class Session:
    def __init__(self, api: Plates, out: Path, style: str, preserve: str) -> None:
        self.api, self.out, self.style, self.preserve = api, out, style, preserve
        self.chain: list[dict] = []
        self.out.mkdir(parents=True, exist_ok=True)

    @property
    def current(self) -> bytes | None:
        return Path(self.chain[-1]["file"]).read_bytes() if self.chain else None

    def _record(self, img: bytes, prompt: str, secs: float, kind: str, model: str) -> None:
        path = self.out / ("%03d.png" % len(self.chain))
        path.write_bytes(img)
        self.chain.append({"file": str(path), "prompt": prompt, "secs": secs,
                           "kind": kind, "model": model})
        (self.out / "chain.json").write_text(json.dumps(self.chain, indent=1), encoding="utf-8")
        print("  %s  %.1fs  %d KB  [%s]" % (path.name, secs, len(img) // 1024, model))

    def base(self, prompt: str, model: str | None = None) -> None:
        full = prompt if not self.style else prompt.rstrip() + " " + self.style
        img, secs = self.api.generate(full, model)
        self.chain.clear()
        self._record(img, prompt, secs, "base", MODELS.get(model or "", model or self.api.model))

    def edit(self, instruction: str, model: str | None = None) -> None:
        cur = self.current
        if cur is None:
            raise PlateError("no plate yet -- give an opening prompt or use :new")
        img, secs = self.api.edit(cur, instruction.rstrip() + self.preserve, model)
        self._record(img, instruction, secs, "edit", MODELS.get(model or "", model or self.api.model))

    def undo(self) -> None:
        if len(self.chain) <= 1:
            print("  nothing to undo"); return
        Path(self.chain.pop()["file"]).unlink(missing_ok=True)
        print("  back to %s" % Path(self.chain[-1]["file"]).name)

    def view(self) -> None:
        page = viewer.write(self.chain, self.out)
        print("  %s   (open it, or: python3 -m http.server -d %s)" % (page, self.out))


def repl(s: Session) -> None:
    try:
        import readline  # noqa: F401  -- history and line editing, if the platform has it
    except ImportError:
        pass
    print("plates ready. type an edit, or :q to quit. :view for the morph.")
    while True:
        try:
            line = input("\nplate> ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); return
        if not line:
            continue
        cmd, _, rest = line.partition(" ")
        rest = rest.strip()
        try:
            if cmd in (":q", ":quit", ":exit"):
                return
            elif cmd == ":view":
                s.view()
            elif cmd == ":ls":
                for i, c in enumerate(s.chain):
                    print("  %2d  %-5s %5.1fs  %s" % (i, c["kind"], c["secs"] or 0, c["prompt"][:70]))
            elif cmd == ":where":
                print("  " + str(s.out))
            elif cmd == ":undo":
                s.undo()
            elif cmd == ":redo":
                last = next((c for c in reversed(s.chain) if c["kind"] == "edit"), None)
                if not last:
                    print("  no edit to redo"); continue
                s.undo(); s.edit(last["prompt"])
            elif cmd == ":new":
                if not rest:
                    print("  :new <prompt>"); continue
                s.base(rest)
            elif cmd == ":model":
                if rest not in MODELS and rest not in MODELS.values():
                    print("  models: " + ", ".join("%s (%s)" % (k, v) for k, v in MODELS.items())); continue
                s.api.model = MODELS.get(rest, rest)
                print("  model -> " + s.api.model)
            elif cmd == ":style":
                s.style = rest
                print("  style -> " + (rest or "(none)"))
            elif cmd.startswith(":"):
                print("  commands: :view :new :model :style :undo :redo :ls :where :q")
            else:
                s.edit(line)
        except PlateError as e:
            print("  ! %s" % e)


def main() -> None:
    ap = argparse.ArgumentParser(description="Prompt-driven plate chain for The Loom.")
    ap.add_argument("prompt", nargs="*", help="opening prompt; omit to start empty")
    ap.add_argument("--model", default="lite", choices=list(MODELS), help="default 'lite' (fits a round)")
    ap.add_argument("--out", default=None, help="output dir (default plates/out/<timestamp>)")
    ap.add_argument("--style", default=STYLE, help="suffix on the opening prompt")
    ap.add_argument("--no-preserve", action="store_true",
                    help="drop the keep-composition clause, to see why it is there")
    ap.add_argument("--edit", action="append", default=[],
                    help="run an edit non-interactively; repeatable, then exits")
    args = ap.parse_args()

    out = Path(args.out) if args.out else OUT / time.strftime("%Y%m%d-%H%M%S")
    try:
        api = Plates(model=args.model)
    except PlateError as e:
        print("! %s\n  export GEMINI_API_KEY=... first" % e, file=sys.stderr)
        raise SystemExit(2)

    s = Session(api, out, args.style, "" if args.no_preserve else PRESERVE)
    print("out: %s\nmodel: %s" % (out, api.model))
    if args.prompt:
        s.base(" ".join(args.prompt))
    for e in args.edit:
        s.edit(e)
    if args.edit:
        s.view(); return
    repl(s)
    if len(s.chain) > 1:
        s.view()


if __name__ == "__main__":
    main()
