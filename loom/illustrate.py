"""Companion stills for the Loom: turn a just-committed paragraph into an illustration.

This talks to a hosted Gemini image-output model ("Nano Banana") over plain HTTP -- stdlib only,
matching the rest of this repo, which has no pyproject.toml/requirements.txt and runs via
`uv run python3 loom/server.py` directly. No `google-genai`, no `requests`.

The call is a best-effort side channel, never the text loop's critical path (see server.py's
`_maybe_illustrate`): a bad key, a rate limit, a network blip, or a response shape that shifted
underneath us all degrade to "no image this round," never a crash. When `previous` bytes are handed
in, the call becomes an image-to-image edit instead of a fresh text-to-image generation -- that's what
keeps the same setting/characters across rounds instead of a new scene every ~10 seconds.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")   # <-- put your Gemini API key here, or export GEMINI_API_KEY
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")  # "Nano Banana"; the
    # exact model id may need to change (e.g. to a "gemini-3.x-flash-image" id) once you confirm what's
    # available on your API key -- this is a placeholder default, override via GEMINI_IMAGE_MODEL.
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

PROMPT = ("Illustrate this scene from a live collaborative story; if a previous image is given, keep "
          "the same setting/characters/mood and only update what changed; no text or captions in the "
          "image.")


class Illustrator:
    def __init__(self, api_key: str = GEMINI_API_KEY, model: str = GEMINI_IMAGE_MODEL) -> None:
        self.api_key = api_key
        self.model = model

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def illustrate(self, text: str, previous: bytes | None = None) -> tuple[bytes, str] | None:
        """One still for `text`. `previous`, if given, seeds an image-to-image edit for continuity
        with the last round's illustration of this paragraph. Never raises -- returns None on any
        failure, because a missing picture is fine and a crashed round loop is not."""
        if not self.enabled:
            return None
        parts = [{"text": f"{PROMPT}\n\n{text}"}]
        if previous is not None:
            parts.append({"inlineData": {"mimeType": "image/png",
                                         "data": base64.b64encode(previous).decode()}})
        body = json.dumps({"contents": [{"parts": parts}]}).encode()
        url = f"{GEMINI_API_BASE}/models/{self.model}:generateContent?key={self.api_key}"
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.load(r)
            for part in resp["candidates"][0]["content"]["parts"]:
                inline = part.get("inlineData") if isinstance(part, dict) else None
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"]), inline.get("mimeType") or "image/png"
            return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                KeyError, IndexError, TypeError, ValueError):
            return None
