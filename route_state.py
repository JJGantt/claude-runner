#!/usr/bin/env python3
"""
Shared routing state for Claude vs Codex, and model selection.

Stores state in a JSON file (path from config):
  { "mode": "claude", "model": "sonnet" }

Supports "switch to claude/codex" and "use opus/sonnet/haiku" in messages.
"""

import json
import re
from pathlib import Path

from config import MODE_FILE

DEFAULT_MODE = "claude"
DEFAULT_MODEL = "sonnet"
VALID_MODES = {"claude", "codex"}
VALID_MODELS = {"sonnet", "opus", "haiku"}

CLAUDE_VARIANTS = {
    "claude",
    "claud",
    "clawd",
    "clawed",
    "cloud",
    "clod",
    "claw-d",
    "claw d",
}

CODEX_VARIANTS = {
    "codex",
    "kodex",
    "kotex",
    "code x",
    "code ex",
}

SWITCH_RE = re.compile(
    r"\bswitch\s+to\s+("
    r"claude|claud|clawd|clawed|cloud|clod|claw[- ]?d|"
    r"codex|kodex|kotex|code\s*x|code\s*ex"
    r")\b",
    re.IGNORECASE,
)

MODEL_RE = re.compile(
    r"\buse\s+(opus|sonnet|haiku)\b",
    re.IGNORECASE,
)


def _load() -> dict:
    if MODE_FILE.exists():
        try:
            return json.loads(MODE_FILE.read_text())
        except Exception:
            pass
    return {}


def _save(data: dict):
    MODE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MODE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(MODE_FILE)


def get_mode() -> str:
    mode = _load().get("mode", DEFAULT_MODE).lower()
    return mode if mode in VALID_MODES else DEFAULT_MODE


def get_model() -> str:
    model = _load().get("model", DEFAULT_MODEL).lower()
    return model if model in VALID_MODELS else DEFAULT_MODEL


def set_mode(mode: str) -> str:
    mode = str(mode).lower().strip()
    if mode not in VALID_MODES:
        raise ValueError(f"Invalid mode: {mode}")
    data = _load()
    data["mode"] = mode
    _save(data)
    return mode


def set_model(model: str) -> str:
    model = str(model).lower().strip()
    if model not in VALID_MODELS:
        raise ValueError(f"Invalid model: {model}")
    data = _load()
    data["model"] = model
    _save(data)
    return model


def apply_switch_and_strip(text: str) -> tuple[str, str | None, str | None]:
    """
    Detect "switch to claude/codex" and/or "use opus/sonnet/haiku" in message.
    Returns (cleaned_text, new_mode_or_None, new_model_or_None).
    """
    if not text:
        return text, None, None

    new_mode = None
    mode_matches = list(SWITCH_RE.finditer(text))
    if mode_matches:
        target = mode_matches[-1].group(1).lower()
        if target in CLAUDE_VARIANTS or target.replace("-", " ") in CLAUDE_VARIANTS:
            new_mode = "claude"
        else:
            new_mode = "codex"
        text = SWITCH_RE.sub("", text).strip()

    new_model = None
    model_matches = list(MODEL_RE.finditer(text))
    if model_matches:
        new_model = model_matches[-1].group(1).lower()
        text = MODEL_RE.sub("", text).strip()

    return text, new_mode, new_model
