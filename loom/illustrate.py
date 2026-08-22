"""Companion stills for the Loom: turn a just-committed paragraph into an illustration.

This talks to a hosted Gemini image-output model ("Nano Banana") over plain HTTP -- no `google-genai`
SDK, no `requests`, just `urllib` -- but does use `certifi` (for a working CA bundle; see below) and
`pillow` (for `_as_reference`'s JPEG re-encode). Both are declared in the repo's pyproject.toml and
managed by `uv run`, which is how loom/server.py is meant to be launched (see run-loom.sh).

The call is a best-effort side channel, never the text loop's critical path (see server.py's
`_maybe_illustrate`): a bad key, a rate limit, a network blip, or a response shape that shifted
underneath us all degrade to "no image this round," never a crash. When `previous` bytes are handed
in, the call becomes an image-to-image edit instead of a fresh text-to-image generation -- that's what
keeps the same setting/characters across rounds instead of a new scene every ~10 seconds.

Model/style choices below are carried over from a teammate's separate prompt-tuning tool
(loom/plates/, on the image_generation branch) rather than re-derived here -- see its module docstrings
for the measurements. Only the choices are borrowed; the round-loop wiring stays this file's own.
"""

from __future__ import annotations

import base64
import io
import json
import os
import random
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

import certifi
from PIL import Image

# This python.org macOS install has no system CA bundle wired into `ssl` by default (the usual "Install
# Certificates.command" fix doesn't exist for every installed Python version) -- certifi's bundle is
# what pip itself already relies on to fetch packages, so it's a safe thing to lean on here too.
_SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def _load_dotenv(path: Path) -> dict[str, str]:
    """A `KEY=value` line reader, nothing more -- no quoting rules, no interpolation. Avoids adding
    python-dotenv as a dependency in a repo that otherwise has none."""
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


_DOTENV = _load_dotenv(Path(__file__).resolve().parent / ".env")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name) or _DOTENV.get(name, default)


# Two keys, picked at random per call (see Illustrator.illustrate) -- gives the demo double the quota
# and headroom against one key hitting a rate limit mid-round. Put them in loom/.env (gitignored):
#   GEMINI_KEY_1=...
#   GEMINI_KEY_2=...
GEMINI_KEYS = [k for k in (_env("GEMINI_KEY_1"), _env("GEMINI_KEY_2")) if k]
# gemini-3.1-flash-lite-image ("Nano Banana 2 Lite"): measured ~3.8s generate / ~7s edit -- the only
# tier that keeps up with a ~6s Loom round without the illustration lagging further and further behind
# the text. gemini-3.1-flash-image and gemini-3-pro-image are slower (~11s / ~18s) but higher quality;
# fine for a one-off (e.g. the time-lapse close) but not for every round.
GEMINI_IMAGE_MODEL = _env("GEMINI_IMAGE_MODEL", "gemini-3.1-flash-lite-image")
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

PROMPT = "Illustrate this scene from a live collaborative story. No text or captions in the image."

# The opening plate establishes the medium; everything after is an edit of it, so the whole chain reads
# as "a painting" only if this one commits to it. 16:9 to match a projector.
STYLE = ("Traditional oil painting on canvas. Visible brushstrokes and impasto ridges catching the "
         "light, the woven texture of the canvas showing through thin passages, soft painterly edges, "
         "layered glazes, muted cold palette, dramatic rim light. This is a painting, not a photograph "
         "and not a 3D render. 16:9.")
# Re-asserted on every edit -- not redundant. An edit instruction describes new *content* ("the fog
# rolls in"), and left to its own idiom the model pulls back toward photorealism for exactly the
# content most likely to appear in this story (water, metal, skin). State the medium once at the start
# and the chain drifts out of paint within a few rounds; state it every time and it holds.
STYLE_HOLD = (" Keep it a traditional oil painting on canvas: visible brushstrokes, impasto texture, "
              "canvas weave, painterly edges. Never photorealistic.")
# What keeps a cross-fade between two plates reading as the world morphing in place rather than a
# slideshow cut: the model re-frames the shot by default unless told not to.
PRESERVE = (" Keep the same character, pose, camera angle, composition and lighting. "
            "Change only what this instruction asks for.")

# The reference image is re-encoded before upload when Pillow is available. PNG cost 8.7-12.7s per
# edit; the same picture as JPEG lands at ~7s -- this is about the encoder, not the pixel count, so
# downscale for bandwidth, not for speed.
_REF_MAX_W = 1024
_REF_QUALITY = 85

_RETRY_STATUS = {429, 500, 503}


def _as_reference(png: bytes) -> tuple[bytes, str]:
    """Re-encode a plate for use as an edit reference. Falls back to the original bytes if the source
    image is somehow malformed -- a slower edit call beats no edit at all."""
    try:
        im = Image.open(io.BytesIO(png)).convert("RGB")
        if im.width > _REF_MAX_W:
            im = im.resize((_REF_MAX_W, round(im.height * _REF_MAX_W / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=_REF_QUALITY)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return png, "image/png"


class Illustrator:
    def __init__(self, api_keys: list[str] | None = None, model: str = GEMINI_IMAGE_MODEL,
                 retries: int = 2) -> None:
        self.api_keys = list(GEMINI_KEYS if api_keys is None else api_keys)
        self.model = model
        self.retries = retries
        self.last_error: str | None = None   # set on failure; never raised, just there to debug by

    @property
    def enabled(self) -> bool:
        return bool(self.api_keys)

    def illustrate(self, text: str, previous: bytes | None = None) -> tuple[bytes, str] | None:
        """One still for `text`. `previous`, if given, seeds an image-to-image edit for continuity
        with the last round's illustration of this paragraph. Never raises -- returns None on any
        failure, because a missing picture is fine and a crashed round loop is not."""
        if not self.enabled:
            return None
        if previous is None:
            # The base plate: establish the medium in full.
            parts = [{"text": f"{PROMPT}\n\n{text}\n\n{STYLE}"}]
        else:
            # An edit: reference first so the instruction reads as a change to the picture rather than
            # a fresh description -- that's what keeps composition stable across the chain.
            ref, mime = _as_reference(previous)
            parts = [{"inlineData": {"mimeType": mime, "data": base64.b64encode(ref).decode()}},
                     {"text": f"{text}{PRESERVE}{STYLE_HOLD}"}]
        body = json.dumps({"contents": [{"parts": parts}]}).encode()
        for attempt in range(self.retries + 1):
            api_key = random.choice(self.api_keys)   # re-picked per attempt: spreads retries across keys
            url = f"{GEMINI_API_BASE}/models/{self.model}:generateContent?key={api_key}"
            try:
                req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as r:
                    resp = json.load(r)
                for part in resp["candidates"][0]["content"]["parts"]:
                    inline = part.get("inlineData") if isinstance(part, dict) else None
                    if inline and inline.get("data"):
                        return base64.b64decode(inline["data"]), inline.get("mimeType") or "image/png"
                self.last_error = "no image in response"
                return None                          # a safety block or text-only reply; retry won't help
            except urllib.error.HTTPError as e:
                self.last_error = f"HTTP {e.code}: {_reason(e.read())}"
                if e.code not in _RETRY_STATUS or attempt == self.retries:
                    return None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                    KeyError, IndexError, TypeError, ValueError) as e:
                self.last_error = str(e)
                if attempt == self.retries:
                    return None
            time.sleep(1.5 * (attempt + 1))
        return None


def _reason(body: bytes) -> str:
    """Google's error body is a nested envelope; the useful sentence is one field down."""
    try:
        err = json.loads(body).get("error", {})
        msg, status = err.get("message") or "", err.get("status") or ""
        return f"{msg} ({status})" if status and msg else (msg or status or "unknown")
    except Exception:
        return body[:160].decode("utf-8", "replace")
