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
import random
import ssl
import urllib.error
import urllib.request
from pathlib import Path

try:
    # This python.org macOS install has no system CA bundle wired into `ssl` by default (the usual
    # "Install Certificates.command" fix doesn't exist for every installed version) -- certifi's
    # bundle is what pip itself already relies on, so it's reliably present without adding a new
    # dependency. Fall back to the interpreter default if it isn't.
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()


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
GEMINI_IMAGE_MODEL = _env("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")  # "Nano Banana"; the exact
    # model id may need to change (e.g. to a "gemini-3.x-flash-image" id) once you confirm what's
    # available on your key -- this is a placeholder default, override via GEMINI_IMAGE_MODEL.
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

PROMPT = ("Illustrate this scene from a live collaborative story; if a previous image is given, keep "
          "the same setting/characters/mood and only update what changed; no text or captions in the "
          "image.")


class Illustrator:
    def __init__(self, api_keys: list[str] | None = None, model: str = GEMINI_IMAGE_MODEL) -> None:
        self.api_keys = list(GEMINI_KEYS if api_keys is None else api_keys)
        self.model = model

    @property
    def enabled(self) -> bool:
        return bool(self.api_keys)

    def illustrate(self, text: str, previous: bytes | None = None) -> tuple[bytes, str] | None:
        """One still for `text`. `previous`, if given, seeds an image-to-image edit for continuity
        with the last round's illustration of this paragraph. Never raises -- returns None on any
        failure, because a missing picture is fine and a crashed round loop is not."""
        if not self.enabled:
            return None
        api_key = random.choice(self.api_keys)
        parts = [{"text": f"{PROMPT}\n\n{text}"}]
        if previous is not None:
            parts.append({"inlineData": {"mimeType": "image/png",
                                         "data": base64.b64encode(previous).decode()}})
        body = json.dumps({"contents": [{"parts": parts}]}).encode()
        url = f"{GEMINI_API_BASE}/models/{self.model}:generateContent?key={api_key}"
        try:
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=30, context=_SSL_CONTEXT) as r:
                resp = json.load(r)
            for part in resp["candidates"][0]["content"]["parts"]:
                inline = part.get("inlineData") if isinstance(part, dict) else None
                if inline and inline.get("data"):
                    return base64.b64decode(inline["data"]), inline.get("mimeType") or "image/png"
            return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError,
                KeyError, IndexError, TypeError, ValueError):
            return None
