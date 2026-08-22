#!/usr/bin/env python3
"""
OpenAI-compatible HTTP front-end for DiffusionGemma 26B-A4B on Jetson Thor.

DiffusionGemma is not supported by llama-server, so this wraps the PR's persistent
`llama-diffusion-gemma-visual-server`, which speaks a line protocol over stdin/stdout:

    stdin : one line = path to a JSON request file
    file  : {"seed": int, "n_blocks": int, "messages": [{"role","content"}, ...],
             optional eb_* entropy-bound overrides}
    stdout: P <block> <json-array>                where this request's pinned spans landed
            F <block> <step> <total> <json-array>  per-denoise-step canvas, one string per position
                                                   (in-place, non-monotonic)
            C <block> <json-str>                  CUMULATIVE committed answer text
            STATS <k=v ...>
            DONE | ERR <msg>

The backend is strictly single-threaded and synchronous, so requests are serialised
behind one lock. On /v1/chat/completions the streaming deltas are derived from `C` records
(monotonic) rather than `F` frames, which mutate in place and would emit garbled text.

Requests may also PIN text into the canvas: `pins: [{pos, text, block, id}]` freezes a phrase at a canvas
position and lets the model denoise the rest of the canvas around it. Attention is non-causal, so the text
before a pin reacts to it too - which is the whole point of doing this on a diffusion model.

`F` frames are the actual denoising canvas and are what makes this model interesting to watch,
so they get their own non-OpenAI endpoint, /v1/diffusion/stream, which passes every frame
through as SSE. The entropy-bound decoder has no mask token: every canvas position always holds
an argmax token, and positions freeze as their entropy drops while the rest are renoised each
step. So the client visualises per-position CHANGE, not mask->token.

Stdlib only - no pip installs.
"""
import json, os, queue, subprocess, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME     = os.path.expanduser("~")
BIN      = f"{HOME}/diffusiongemma/llama.cpp/build/bin/llama-diffusion-gemma-visual-server"
MODEL    = f"{HOME}/diffusiongemma/models/diffusiongemma-26B-A4B-it-GGUF/diffusiongemma-26B-A4B-it-Q8_0.gguf"
REQDIR   = f"{HOME}/diffusiongemma/.requests"
LOG      = f"{HOME}/diffusiongemma/backend.log"
MODEL_ID = "diffusiongemma-26B-A4B-it"
CANVAS   = 256          # tokens per diffusion block

HOST = os.environ.get("DG_HOST", "100.70.13.60")   # Tailscale IP only - never 0.0.0.0
PORT = int(os.environ.get("DG_PORT", "8080"))
NGL  = os.environ.get("NGL", "99")


class Backend:
    """Owns the model subprocess. One request at a time, enforced by self.lock."""

    def __init__(self):
        os.makedirs(REQDIR, exist_ok=True)
        self.lock = threading.Lock()
        self.log = open(LOG, "ab", buffering=0)
        env = dict(os.environ, NGL=NGL)
        print(f"[dg] loading {os.path.basename(MODEL)} (NGL={NGL}) ...", flush=True)
        self.p = subprocess.Popen(
            [BIN, MODEL], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.log, text=True, bufsize=1, env=env,
        )
        line = self._readline_startup()
        if not line.startswith("READY"):
            raise RuntimeError(f"backend failed to start; see {LOG}. got: {line!r}")
        parts = line.split()
        self.n_vocab, self.maxtok = int(parts[1]), int(parts[2])
        print(f"[dg] READY n_vocab={self.n_vocab} maxtok={self.maxtok}", flush=True)

    def _readline_startup(self):
        # model load can take ~40s; just block on the pipe
        while True:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError(f"backend died during load; see {LOG}")
            line = line.strip()
            if line:
                return line

    def generate(self, messages, n_blocks, seed, eb=None, frames=False, pins=None):
        """Yield ('full', cumulative_text) as blocks commit, then ('stats', dict).

        With frames=True, also yield ('frame', {block, step, total, tokens}) for every denoising
        step. Callers that ignore frames must still match on the kind, not on 'not full'.

        `pins` freezes spans into the canvas; each block that has any yields ('pins', {block, spans})
        before its first frame, reporting where they landed once tokenized."""
        with self.lock:
            if self.p.poll() is not None:
                raise RuntimeError("backend process is dead")
            rid = uuid.uuid4().hex
            path = os.path.join(REQDIR, f"{rid}.json")
            req = {"seed": seed, "n_blocks": n_blocks, "messages": messages}
            if pins:
                req["pins"] = pins
            req.update(eb or {})
            with open(path, "w") as f:
                json.dump(req, f)
            self.p.stdin.write(path + "\n")
            self.p.stdin.flush()

            # Resynchronise: skip anything still in the pipe from an abandoned earlier request until
            # the backend echoes OUR request id. Without this a single disconnected client leaves the
            # stream one request out of step for the lifetime of the process.
            want = "BEGIN " + path
            while True:
                line = self.p.stdout.readline()
                if not line:
                    raise RuntimeError(f"backend died before ack; see {LOG}")
                line = line.rstrip("\n")
                if line == want:
                    break
                if line.startswith("BEGIN "):
                    raise RuntimeError("backend acked a different request; stream desynchronised")

            stats = {}
            finished = False
            try:
                while True:
                    line = self.p.stdout.readline()
                    if not line:
                        raise RuntimeError(f"backend died mid-request; see {LOG}")
                    line = line.rstrip("\n")
                    if line.startswith("C "):
                        # C <block> <json-string>  -- cumulative committed text
                        _, _, rest = line.split(" ", 2)
                        yield ("full", json.loads(rest))
                    elif line.startswith("P "):
                        # P <block> <json-array>  -- resolved pin spans, before this block's first frame
                        _, b, rest = line.split(" ", 2)
                        yield ("pins", {"block": int(b), "spans": json.loads(rest)})
                    elif line.startswith("F "):
                        if not frames:
                            continue              # in-place canvas frame; not monotonic
                        _, b, st, tot, rest = line.split(" ", 4)
                        toks = json.loads(rest)
                        if isinstance(toks, str):
                            toks = [toks]         # pre-per-token binary; degrade to one big cell
                        yield ("frame", {"block": int(b), "step": int(st),
                                         "total": int(tot), "tokens": toks})
                    elif line.startswith("STATS "):
                        for kv in line[6:].split():
                            if "=" in kv:
                                k, v = kv.split("=", 1)
                                stats[k] = v
                    elif line == "DONE":
                        finished = True
                        break
                    elif line.startswith("ERR "):
                        finished = True          # ERR is terminal for this request
                        raise RuntimeError(line[4:])
            finally:
                # A client that hangs up mid-generation closes this generator at its yield, which lands
                # here. The backend is still producing records for this request, so read them to DONE
                # before releasing the lock rather than leaving them for the next caller.
                if not finished:
                    self._drain()
                try:
                    os.unlink(path)
                except OSError:
                    pass
            yield ("stats", stats)

    def tokenize(self, texts, special=False):
        """Token pieces for each text, from the backend's own vocab. The pin planner needs exact token
        counts to lay out spans, and shipping a tokenizer to the client would be a second source of
        truth for something the model already owns."""
        with self.lock:
            if self.p.poll() is not None:
                raise RuntimeError("backend process is dead")
            rid = uuid.uuid4().hex
            path = os.path.join(REQDIR, f"{rid}.json")
            with open(path, "w") as f:
                json.dump({"op": "tokenize", "texts": list(texts), "special": bool(special)}, f)
            self.p.stdin.write(path + "\n")
            self.p.stdin.flush()
            want = "BEGIN " + path
            out = None
            try:
                while True:
                    line = self.p.stdout.readline()
                    if not line:
                        raise RuntimeError(f"backend died during tokenize; see {LOG}")
                    line = line.rstrip("\n")
                    if line == want or not line:
                        continue
                    if line.startswith("BEGIN "):
                        raise RuntimeError("backend acked a different request; stream desynchronised")
                    if line.startswith("T "):
                        out = json.loads(line[2:])
                    elif line == "DONE":
                        break
                    elif line.startswith("ERR "):
                        raise RuntimeError(line[4:])
            finally:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            if out is None:
                raise RuntimeError("backend returned no tokenization")
            return out

    def _drain(self):
        """Read the rest of the in-flight response and throw it away. Bounded by the backend itself:
        it always terminates a request with DONE or ERR."""
        n = 0
        while True:
            line = self.p.stdout.readline()
            if not line:
                break
            n += 1
            line = line.rstrip("\n")
            if line == "DONE" or line.startswith("ERR "):
                break
        if n:
            print(f"[dg] client hung up; drained {n} backend records", flush=True)

    def close(self):
        try:
            self.p.stdin.write("QUIT\n"); self.p.stdin.flush()
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()


T_OPEN, T_CLOSE = "<|channel>", "<channel|>"
KEEP_CHANNELS = os.environ.get("DG_KEEP_CHANNELS") == "1"


def split_thought(text):
    """DiffusionGemma wraps reasoning in the special tokens <|channel> ... <channel|>.
    Returns (reasoning, answer, closed).

    The closing <channel|> is frequently NOT emitted - the model uses its whole canvas
    thinking and the useful answer is the tail of that thought. So `closed` reports
    whether an authoritative boundary was actually seen; callers must not hand an empty
    answer to a client just because the marker was missing."""
    if KEEP_CHANNELS:
        return "", text, True

    def strip_open(h):
        if h.startswith(T_OPEN):
            h = h[len(T_OPEN):]
            if h.startswith("thought"):
                h = h[len("thought"):]
            h = h.lstrip("\n")
        return h

    i = text.find(T_CLOSE)
    if i == -1:
        if text.startswith(T_OPEN):
            return strip_open(text), "", False     # still inside the thought channel
        return "", text, True                     # plain answer, no channels at all
    return strip_open(text[:i]), text[i + len(T_CLOSE):], True


BACKEND = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "diffusiongemma-shim/1.0"

    def log_message(self, fmt, *a):
        sys.stderr.write("[dg] %s - %s\n" % (self.address_string(), fmt % a))

    # ---- helpers ----
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code, msg, typ="invalid_request_error"):
        self._json(code, {"error": {"message": msg, "type": typ, "code": None, "param": None}})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/healthz"):
            alive = BACKEND.p.poll() is None
            return self._json(200 if alive else 503,
                              {"status": "ok" if alive else "backend_dead",
                               "model": MODEL_ID, "maxtok": BACKEND.maxtok})
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            return self._json(200, {"object": "list", "data": [
                {"id": MODEL_ID, "object": "model", "created": int(time.time()),
                 "owned_by": "google/unsloth"}]})
        return self._err(404, f"unknown path {self.path}")

    # ---- request parsing shared by both POST endpoints ----
    EB_KEYS = {
        "eb_max_steps": int, "eb_stability": int,
        "eb_entropy_bound": float, "eb_t_max": float, "eb_t_min": float, "eb_confidence": float,
    }

    def _read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    @staticmethod
    def _norm_messages(messages):
        """OpenAI messages -> the backend's plain-string content."""
        norm = []
        for m in messages:
            c = m.get("content", "")
            if isinstance(c, list):   # OpenAI multipart -> concatenate text parts
                c = "".join(p.get("text", "") for p in c if isinstance(p, dict))
            norm.append({"role": m.get("role", "user"), "content": c})
        return norm

    @classmethod
    def _eb_overrides(cls, req):
        """Pull the entropy-bound knobs the client set; anything absent keeps the model default."""
        eb = {}
        for k, cast in cls.EB_KEYS.items():
            if req.get(k) is not None:
                try:
                    eb[k] = cast(req[k])
                except (TypeError, ValueError):
                    pass
        return eb

    @staticmethod
    def _blocks_for(req):
        max_tok = req.get("max_tokens") or req.get("max_completion_tokens") or 2048
        try:
            max_tok = max(1, int(max_tok))
        except Exception:
            max_tok = 2048
        return max(1, -(-max_tok // CANVAS))       # ceil

    @staticmethod
    def _pins_for(req):
        """Canvas positions to freeze: [{pos, text, block, id}]. The backend tokenizes `text`, so a
        mid-sentence phrase should carry its leading space. A malformed entry is dropped rather than
        failing the request - a room full of phones is not a well-behaved client."""
        out = []
        for p in req.get("pins") or []:
            if not isinstance(p, dict):
                continue
            text = p.get("text")
            if not isinstance(text, str) or not text:
                continue
            try:
                pin = {"pos": int(p.get("pos", 0)), "text": text, "block": int(p.get("block", 0))}
            except (TypeError, ValueError):
                continue
            if p.get("id") is not None:
                pin["id"] = str(p["id"])
            if p.get("special"):
                pin["special"] = True     # trusted caller only: lets this span carry control tokens
            out.append(pin)
        return out

    @staticmethod
    def _seed_for(req):
        seed = req.get("seed")
        return int(seed) if isinstance(seed, (int, float)) else int(time.time()) & 0x7fffffff

    def do_POST(self):
        path = self.path.rstrip("/")
        if path in ("/v1/diffusion/stream", "/diffusion/stream"):
            return self._diffusion()
        if path in ("/v1/tokenize", "/tokenize"):
            return self._tokenize()
        if path not in ("/v1/chat/completions", "/chat/completions"):
            return self._err(404, f"unknown path {self.path}")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as e:
            return self._err(400, f"bad JSON: {e}")

        messages = req.get("messages")
        if not isinstance(messages, list) or not messages:
            return self._err(400, "'messages' must be a non-empty array")
        norm = self._norm_messages(messages)
        n_blocks = self._blocks_for(req)
        seed = self._seed_for(req)
        eb = self._eb_overrides(req)
        pins = self._pins_for(req)
        stream = bool(req.get("stream"))

        cid = "chatcmpl-" + uuid.uuid4().hex[:24]
        created = int(time.time())

        if stream:
            return self._stream(cid, created, norm, n_blocks, seed, eb, pins)
        return self._blocking(cid, created, norm, n_blocks, seed, eb, pins)

    def _tokenize(self):
        """Token pieces for a list of strings. Not an OpenAI endpoint: it exists because pin layout is
        computed client-side, and only the backend knows how many positions a span will occupy."""
        try:
            req = self._read_json()
        except Exception as e:
            return self._err(400, f"bad JSON: {e}")
        texts = req.get("texts")
        if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
            return self._err(400, "'texts' must be an array of strings")
        try:
            pieces = BACKEND.tokenize(texts, bool(req.get("special")))
        except Exception as e:
            return self._err(500, str(e), "server_error")
        return self._json(200, {"tokens": pieces, "counts": [len(p) for p in pieces]})

    # ---- the denoising view: every canvas frame, not just committed text ----
    def _diffusion(self):
        """SSE of the raw denoise: one event per denoising step, plus commits and stats.

        Deliberately not OpenAI-shaped - there is no chat-completions concept for "the canvas
        changed in place". Events:
            {"type":"start",  "block_tokens":256, "n_blocks":n, "seed":s}
            {"type":"pins",   "block":b, "spans":[{"pos":i, "len":n, "text":str, "id":str}, ...]}
            {"type":"frame",  "block":b, "step":i, "total":n, "tokens":[str, ...]}
            {"type":"commit", "block":b, "reasoning":str, "answer":str, "raw":str}
            {"type":"stats",  ...}   {"type":"done"}   {"type":"error", "message":str}
        """
        try:
            req = self._read_json()
        except Exception as e:
            return self._err(400, f"bad JSON: {e}")
        messages = req.get("messages")
        if not isinstance(messages, list) or not messages:
            return self._err(400, "'messages' must be a non-empty array")

        norm     = self._norm_messages(messages)
        n_blocks = self._blocks_for(req)
        seed     = self._seed_for(req)
        eb       = self._eb_overrides(req)
        pins     = self._pins_for(req)
        # Frames are ~1 per denoising step per block; a client that only wants a coarse animation
        # can subsample here rather than pulling every frame over the wire.
        try:
            every = max(1, int(req.get("frame_every") or 1))
        except (TypeError, ValueError):
            every = 1

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        def sse(obj):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()

        try:
            sse({"type": "start", "block_tokens": CANVAS, "n_blocks": n_blocks,
                 "seed": seed, "model": MODEL_ID, "eb": eb})
            for kind, payload in BACKEND.generate(norm, n_blocks, seed, eb, frames=True, pins=pins):
                if kind == "pins":
                    sse({"type": "pins", **payload})
                elif kind == "frame":
                    # always send the first and last step of a block so the animation has a
                    # clean start and a settled final state even when subsampling
                    last = payload["step"] >= payload["total"] - 1
                    if payload["step"] % every and not last and payload["step"]:
                        continue
                    payload["type"] = "frame"
                    sse(payload)
                elif kind == "full":
                    reasoning, answer, closed = split_thought(payload)
                    if not answer.strip():
                        reasoning, answer = "", reasoning or payload
                    sse({"type": "commit", "reasoning": reasoning,
                         "answer": answer, "raw": payload})
                else:
                    sse({"type": "stats", **payload})
            sse({"type": "done"})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass    # client hung up
        except Exception as e:
            try:
                sse({"type": "error", "message": str(e)})
                self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
            except Exception:
                pass

    def _blocking(self, cid, created, messages, n_blocks, seed, eb=None, pins=None):
        text, stats = "", {}
        try:
            for kind, payload in BACKEND.generate(messages, n_blocks, seed, eb, pins=pins):
                if kind == "full":
                    text = payload
                elif kind == "stats":
                    stats = payload
        except Exception as e:
            return self._err(500, str(e), "server_error")
        reasoning, answer, closed = split_thought(text)
        if not answer.strip():
            # thought channel never closed -> the thought tail IS the answer
            reasoning, answer = "", reasoning or text
        pt = int(stats.get("prompt_n", 0) or 0)
        ct = int(stats.get("predicted_n", 0) or 0)
        msg = {"role": "assistant", "content": answer}
        if reasoning.strip():
            msg["reasoning_content"] = reasoning
        self._json(200, {
            "id": cid, "object": "chat.completion", "created": created, "model": MODEL_ID,
            "choices": [{"index": 0, "finish_reason": "stop", "message": msg}],
            "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
            "x_diffusion_stats": stats,
        })

    def _stream(self, cid, created, messages, n_blocks, seed, eb=None, pins=None):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        def sse(obj):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
            self.wfile.flush()

        def chunk(delta, finish=None):
            return {"id": cid, "object": "chat.completion.chunk", "created": created,
                    "model": MODEL_ID,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        try:
            sse(chunk({"role": "assistant", "content": ""}))
            sent_r = sent_c = ""
            for kind, payload in BACKEND.generate(messages, n_blocks, seed, eb, pins=pins):
                if kind != "full":
                    continue
                reasoning, answer, closed = split_thought(payload)
                d = {}
                if reasoning.startswith(sent_r) and len(reasoning) > len(sent_r):
                    d["reasoning_content"] = reasoning[len(sent_r):]
                elif reasoning != sent_r:
                    d["reasoning_content"] = reasoning
                sent_r = reasoning
                if answer.startswith(sent_c) and len(answer) > len(sent_c):
                    d["content"] = answer[len(sent_c):]
                elif answer != sent_c:
                    d["content"] = answer
                sent_c = answer
                if d:
                    sse(chunk(d))
            if not sent_c.strip() and sent_r.strip():
                # never left the thought channel; give the client the text as content
                sse(chunk({"content": sent_r}))
            sse(chunk({}, "stop"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass    # client hung up
        except Exception as e:
            try:
                sse({"error": {"message": str(e), "type": "server_error"}})
                self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
            except Exception:
                pass


def main():
    global BACKEND
    if not os.path.exists(BIN):
        sys.exit(f"missing backend binary: {BIN}")
    if not os.path.exists(MODEL):
        sys.exit(f"missing model: {MODEL}")
    BACKEND = Backend()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"[dg] OpenAI-compatible API on http://{HOST}:{PORT}/v1  (model={MODEL_ID})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[dg] shutting down", flush=True)
    finally:
        srv.server_close()
        BACKEND.close()


if __name__ == "__main__":
    main()
