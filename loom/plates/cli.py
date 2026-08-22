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

# The opening plate establishes the medium. Everything downstream is an edit of this, so if the first
# plate is not convincingly a painting, nothing later will be either.
STYLE = ("Traditional oil painting on canvas. Visible brushstrokes and impasto ridges catching the "
         "light, the woven texture of the canvas showing through thin passages, soft painterly edges, "
         "layered glazes, muted cold palette, dramatic rim light. This is a painting, not a photograph "
         "and not a 3D render. 16:9.")

# Re-asserted on every edit, and this is not redundant. An edit instruction describes *content* ("he is
# a robot"), and the model will happily render new content in its own default idiom -- metal and water
# in particular pull hard toward photorealism. State the medium once at the start and the chain drifts
# out of paint within a few beats; state it every time and it holds. Kept short so it does not compete
# with the instruction itself for attention.
STYLE_HOLD = (" Keep it a traditional oil painting on canvas: visible brushstrokes, impasto texture, "
              "canvas weave, painterly edges. Never photorealistic.")

OUT = Path(__file__).resolve().parent / "out"


class Session:
    def __init__(self, api: Plates, out: Path, style: str, preserve: str, hold: str) -> None:
        self.api, self.out, self.style, self.preserve, self.hold = api, out, style, preserve, hold
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
        (self.out / "chain.json").write_text(json.dumps(viewer.slim(self.chain), indent=1),
                                             encoding="utf-8")
        viewer.write(self.chain, self.out)
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
        img, secs = self.api.edit(cur, instruction.rstrip() + self.preserve + self.hold, model)
        self._record(img, instruction, secs, "edit", MODELS.get(model or "", model or self.api.model))

    def undo(self) -> None:
        if len(self.chain) <= 1:
            print("  nothing to undo"); return
        Path(self.chain.pop()["file"]).unlink(missing_ok=True)
        (self.out / "chain.json").write_text(json.dumps(viewer.slim(self.chain), indent=1),
                                             encoding="utf-8")
        viewer.write(self.chain, self.out)
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
        except KeyboardInterrupt:
            print("\n  ^C -- plate cancelled, session kept")


def serve(directory: Path, port: int) -> str:
    """Background HTTP server over the plate directory. Localhost only -- nothing here is for the tailnet."""
    import functools
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    import threading

    class Quiet(SimpleHTTPRequestHandler):
        # The page polls twice a second; logging that would bury the prompt you are typing at.
        def log_message(self, *a, **k):
            pass

    handler = functools.partial(Quiet, directory=str(directory))
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), handler)
            break
        except OSError:
            continue
    else:
        return "no free port near %d" % port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return "http://127.0.0.1:%d" % p


def main() -> None:
    ap = argparse.ArgumentParser(description="Prompt-driven plate chain for The Loom.")
    ap.add_argument("prompt", nargs="*", help="opening prompt; omit to start empty")
    ap.add_argument("--model", default="lite", choices=list(MODELS), help="default 'lite' (fits a round)")
    ap.add_argument("--out", default=None, help="output dir (default plates/out/<timestamp>)")
    ap.add_argument("--style", default=STYLE, help="suffix on the opening prompt")
    ap.add_argument("--no-preserve", action="store_true",
                    help="drop the keep-composition clause, to see why it is there")
    ap.add_argument("--no-style-hold", action="store_true",
                    help="stop re-asserting the painted medium on each edit, to watch it drift")
    # --serve takes no value: an optional-value flag would swallow the prompt that follows it.
    ap.add_argument("--serve", action="store_true",
                    help="serve the viewer on 127.0.0.1 and live-update it as plates land")
    ap.add_argument("--port", type=int, default=8090, metavar="N",
                    help="port for --serve (default 8090; scans upward if busy)")
    ap.add_argument("--edit", action="append", default=[],
                    help="run an edit non-interactively; repeatable, then exits")
    args = ap.parse_args()

    out = Path(args.out) if args.out else OUT / time.strftime("%Y%m%d-%H%M%S")
    try:
        api = Plates(model=args.model)
    except PlateError as e:
        print("! %s\n  export GEMINI_API_KEY=... first" % e, file=sys.stderr)
        raise SystemExit(2)

    s = Session(api, out, args.style, "" if args.no_preserve else PRESERVE,
                "" if args.no_style_hold else STYLE_HOLD)
    print("out: %s\nmodel: %s" % (out, api.model))
    viewer.write(s.chain, s.out)           # exists before the first plate, so the page can be opened now
    (s.out / "chain.json").write_text("[]", encoding="utf-8")
    if args.serve:
        print("view: %s   (live -- new plates appear on their own)" % serve(s.out, args.port))
    try:
        if args.prompt:
            s.base(" ".join(args.prompt))
        for e in args.edit:
            s.edit(e)
    except KeyboardInterrupt:
        print("\n  ^C -- stopped")
        if s.chain:
            s.view()
        return
    except PlateError as e:
        print("  ! %s" % e)
        raise SystemExit(1)
    if args.edit:
        s.view(); return
    repl(s)
    if len(s.chain) > 1:
        s.view()


if __name__ == "__main__":
    main()
