"""Gemini image plates for The Loom.

One plate per story beat. A plate is produced by *editing* the previous plate rather than generating a
fresh image, because consecutive edits come back with near-identical composition -- same trees, same
pose, same light -- and only the thing you asked to change changes. That is what makes a cross-dissolve
between two plates read as the world morphing in place instead of as a slideshow cut. Generating each
plate independently loses that and the protagonist gets a new face every round.

Measured on gemini-3.1-flash-lite-image, 1376x768:

    generate  3.8 s        edit  ~7 s        four chained edits, no visible drift

Model choice is a latency decision, not a taste one. A Loom round is ~6 s (see ../../docs/backend.md),
so only the Lite model keeps up; Pro is for the final plate, when nothing is waiting on it.
"""

from __future__ import annotations

import base64
import io
import json
import os
import time
import urllib.error
import urllib.request

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/"

# Measured on this key. `edit` is blank where it was never the point -- anything slower than Lite is
# already too slow to sit on the round loop, so only its generate time matters.
MODELS = {
    "lite":  "gemini-3.1-flash-lite-image",   # Nano Banana 2 Lite -- 3.8 s gen, ~7 s edit. The round loop.
    "flash": "gemini-3.1-flash-image",        # Nano Banana 2      -- 11.2 s gen.
    "pro":   "gemini-3-pro-image",            # Nano Banana Pro    -- 17.7 s gen. The final plate.
}
DEFAULT = "lite"

# The reference image is re-encoded before upload. PNG cost 8.7-12.7 s per edit; the same picture as
# JPEG lands at ~7 s. Resolution is almost free below that -- 1376px and 768px both came back at ~7 s --
# so this is about the encoder, not the pixel count. Downscale for bandwidth, not for speed.
REF_MAX_W = 1024
REF_QUALITY = 85

try:
    from PIL import Image
    HAVE_PIL = True
except ImportError:          # the rest of the repo is stdlib-only; degrade rather than hard-depend
    HAVE_PIL = False


class PlateError(RuntimeError):
    pass


def as_reference(png: bytes) -> tuple[bytes, str]:
    """Re-encode a plate for use as an edit reference. Falls back to the original bytes without PIL."""
    if not HAVE_PIL:
        return png, "image/png"
    try:
        im = Image.open(io.BytesIO(png)).convert("RGB")
        if im.width > REF_MAX_W:
            im = im.resize((REF_MAX_W, round(im.height * REF_MAX_W / im.width)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=REF_QUALITY)
        return buf.getvalue(), "image/jpeg"
    except Exception:
        return png, "image/png"


class Plates:
    """Thin client over the Gemini image models. Synchronous; call it from a worker thread."""

    def __init__(self, api_key: str | None = None, model: str = DEFAULT, timeout: int = 180,
                 retries: int = 2) -> None:
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        if not self.api_key:
            raise PlateError("GEMINI_API_KEY is unset")
        self.model = MODELS.get(model, model)
        self.timeout = timeout
        self.retries = retries

    def generate(self, prompt: str, model: str | None = None) -> tuple[bytes, float]:
        """Make a plate from text alone. Used once, for the opening beat."""
        return self._call([{"text": prompt}], model)

    def edit(self, plate: bytes, instruction: str, model: str | None = None) -> tuple[bytes, float]:
        """Make the next plate by editing this one.

        The reference goes first: the instruction reads as a change to the picture rather than as a
        fresh description, which is what keeps composition stable across the chain."""
        ref, mime = as_reference(plate)
        return self._call([{"inline_data": {"mime_type": mime, "data": base64.b64encode(ref).decode()}},
                           {"text": instruction}], model)

    def _call(self, parts: list[dict], model: str | None) -> tuple[bytes, float]:
        name = MODELS.get(model, model) if model else self.model
        url = ENDPOINT + name + ":generateContent?key=" + self.api_key
        body = json.dumps({"contents": [{"parts": parts}]}).encode()
        started = time.time()
        last = ""
        for attempt in range(self.retries + 1):
            try:
                req = urllib.request.Request(url, body, {"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    data = json.loads(r.read())
                img = _first_image(data)
                if img:
                    return img, time.time() - started
                # A safety block or a text-only reply. Retrying rarely helps, so surface it.
                raise PlateError("no image in response: " + json.dumps(data)[:300])
            except urllib.error.HTTPError as e:
                last = "HTTP %s %s" % (e.code, e.read()[:200].decode("utf-8", "replace"))
                if e.code not in (429, 500, 503) or attempt == self.retries:
                    raise PlateError(last) from None
            except urllib.error.URLError as e:
                last = str(e)
                if attempt == self.retries:
                    raise PlateError(last) from None
            time.sleep(1.5 * (attempt + 1))
        raise PlateError(last or "unreachable")


def _first_image(data: dict) -> bytes | None:
    for cand in data.get("candidates", []):
        for part in cand.get("content", {}).get("parts", []):
            blob = part.get("inlineData") or part.get("inline_data")
            if blob and blob.get("data"):
                return base64.b64decode(blob["data"])
    return None
