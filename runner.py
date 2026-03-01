#!/usr/bin/env python3
"""
Shared message runner for all pipelines (Telegram bot, HTTP server, etc.).

HISTORY SYSTEM
==============
Every exchange is saved to a per-day JSON file via mcp-history.

CONTEXT INJECTION
=================
When a new message arrives, history from the same channel is injected so the
model sees the same conversation the user sees.

Channel mapping:
    sonnet-telegram   →  "sonnet-telegram" channel (isolated)
    opus-telegram     →  "opus-telegram" channel (isolated)
    haiku-telegram    →  "haiku-telegram" channel (isolated)
    codex-telegram    →  "codex-telegram" channel (isolated)
    pi-telegram       →  "pi-telegram" channel (receives ALL context)
    claude-http, codex-http  →  "http" channel
    claude-mac, codex-mac, claude-pi, codex-pi  →  "mac" channel
    claude-voice      →  "voice" channel
    claude-ambient    →  "ambient" channel

Time gaps of 30+ minutes get a visual separator so the model understands
they may be distinct topics.
"""

import json
import os
import subprocess
import logging
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

from config import MCP_HISTORY_DIR, HOME_DIR, MAX_CONTEXT_CHARS, CONTEXT_HOURS

# Import shared history I/O from mcp-history using importlib to avoid
# module name collisions (mcp-history has its own config.py).
def _import_history_io():
    _cfg_spec = importlib.util.spec_from_file_location(
        "mcp_history_config", MCP_HISTORY_DIR / "config.py")
    _cfg_mod = importlib.util.module_from_spec(_cfg_spec)
    import sys as _sys
    _sys.modules["mcp_history_config"] = _cfg_mod
    # Temporarily alias so history_io's "from config import ..." resolves
    _orig_config = _sys.modules.get("config")
    _sys.modules["config"] = _cfg_mod
    _cfg_spec.loader.exec_module(_cfg_mod)
    # Now load history_io
    _io_spec = importlib.util.spec_from_file_location(
        "mcp_history_io", MCP_HISTORY_DIR / "history_io.py")
    _io_mod = importlib.util.module_from_spec(_io_spec)
    _io_spec.loader.exec_module(_io_mod)
    # Restore our own config
    if _orig_config is not None:
        _sys.modules["config"] = _orig_config
    else:
        del _sys.modules["config"]
    return _io_mod

_history_io = _import_history_io()
_mcp_append_entry = _history_io.append_entry
_mcp_load_range = _history_io.load_history_range
_mcp_load_summaries = _history_io.load_summaries_range

RECENT_RAW_COUNT = 10  # Always include this many recent raw exchanges

log = logging.getLogger(__name__)

# Map each source to its channel for context filtering.
# Sources in the same channel share injected context.
_SOURCE_TO_CHANNEL = {
    # Specialized Telegram bots — each isolated to their own history
    "sonnet-telegram":  "sonnet-telegram",
    "opus-telegram":    "opus-telegram",
    "haiku-telegram":   "haiku-telegram",
    "codex-telegram":   "codex-telegram",
    # General Pi bot — stored in own channel, receives ALL context (see _ALL_CONTEXT_SOURCES)
    "pi-telegram":      "pi-telegram",
    # HTTP
    "claude-http":      "http",
    "codex-http":       "http",
    # Mac (Pi interactive SSH sessions merged into Mac channel)
    "claude-mac":       "mac",
    "codex-mac":        "mac",
    "claude-pi":        "mac",
    "codex-pi":         "mac",
    # Voice
    "claude-voice":     "voice",
    # Ambient (wake-word-free voice)
    "claude-ambient":   "ambient",
    # Legacy names (for old history entries still in the 24h window)
    "claude-telegram":  "telegram",
    "telegram":         "telegram",
    "http":             "http",
    "laptop":           "mac",
    "interactive":      "pi",
}

# Sources that receive full cross-channel context (not filtered to their own channel)
_ALL_CONTEXT_SOURCES = {"pi-telegram"}

# Minimum gap (seconds) between entries before inserting a time separator
_GAP_THRESHOLD_SECS = 30 * 60  # 30 minutes


# ---------------------------------------------------------------------------
# History I/O — delegated to mcp-history
# ---------------------------------------------------------------------------

def append_exchange(source: str, user_msg: str, claude_response: str,
                    trace: list | None = None, has_tool_use: bool = False):
    """Append a single exchange to today's history file via mcp-history."""
    extra = {"has_tool_use": has_tool_use}
    if trace is not None:
        extra["trace"] = trace
    _mcp_append_entry(source, user_msg, claude_response, **extra)


def load_history_range(start: datetime, end: datetime) -> list:
    """Return all exchanges between start and end (inclusive by day)."""
    return _mcp_load_range(start, end)


def load_recent_context(hours: int | None = None, channel: str | None = None) -> list:
    """Return exchanges from the last N hours for context injection.

    Args:
        hours:   How far back to look. Defaults to config CONTEXT_HOURS.
        channel: If set, only return entries whose source belongs to this
                 channel. If None, returns all real sources.
    """
    if hours is None:
        hours = CONTEXT_HOURS
    now = datetime.now()
    raw = load_history_range(now - timedelta(hours=hours), now)
    cleaned = []
    for entry in raw:
        src = entry.get("source", "")
        entry_channel = _SOURCE_TO_CHANNEL.get(src)

        # Skip system-error and unrecognised sources
        if entry_channel is None:
            continue

        # Channel filter
        if channel is not None and entry_channel != channel:
            continue

        # Skip entries where the "user" field is actually a meta-prompt
        user = entry.get("user", "").lstrip(" -\n")
        if user.startswith("Recent conversation history") or \
           user.startswith("A previous Claude subprocess") or \
           user.startswith("The Codex CLI subprocess") or \
           user.startswith("You are responding to"):
            continue

        cleaned.append(entry)
    return cleaned


def _source_channel(source: str) -> str | None:
    """Return the channel for a given source, or None."""
    ch = _SOURCE_TO_CHANNEL.get(source)
    if ch is not None:
        return ch
    if source.startswith("claude-ambient-"):
        return "ambient"
    return None


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------

def _format_gap(seconds: int) -> str:
    """Human-readable time gap string."""
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} minutes"
    hours = minutes // 60
    remaining = minutes % 60
    if remaining == 0:
        suffix = "s" if hours != 1 else ""
        return f"{hours} hour{suffix}"
    return f"{hours}h {remaining}m"


_CONTEXT_FRAMING = """\
You are responding to a message from Jared sent via a headless pipeline \
(Telegram or HTTP) on a Raspberry Pi. The conversation below is recent \
context from this channel.

Treat this as the active thread:
- If Jared references earlier messages, resolve that from this context first.
- Do not claim you lack memory when relevant details are present below.
- Use external history tools only when this context is insufficient.

Use it for continuity but do not reference this framing — respond naturally \
as if continuing a normal conversation.

---
Previous conversation (oldest -> newest):\
"""


def _build_system_context(context: list) -> str | None:
    """Build system prompt context string from recent history. Returns None if empty."""
    if not context:
        return None

    context = sorted(context, key=lambda e: e.get("timestamp", ""))

    selected_reversed = []
    total = 0
    for entry in reversed(context):
        ts = datetime.fromisoformat(entry["timestamp"])
        ts_str = ts.strftime("%Y-%m-%d %H:%M")
        user = entry["user"][:450]
        claude = entry["claude"][:450]
        block = f"[{ts_str}] Jared: {user}\n[{ts_str}] Response: {claude}"
        if total + len(block) > MAX_CONTEXT_CHARS and selected_reversed:
            break
        selected_reversed.append(entry)
        total += len(block)

    selected = list(reversed(selected_reversed))

    context_lines = []
    prev_ts = None
    for entry in selected:
        ts = datetime.fromisoformat(entry["timestamp"])
        ts_str = ts.strftime("%Y-%m-%d %H:%M")
        user = entry["user"][:450]
        claude = entry["claude"][:450]
        if prev_ts is not None:
            gap_secs = (ts - prev_ts).total_seconds()
            if gap_secs >= _GAP_THRESHOLD_SECS:
                context_lines.append(f"\n--- {_format_gap(int(gap_secs))} later ---\n")
        context_lines.append(f"[{ts_str}] Jared: {user}\n[{ts_str}] Response: {claude}")
        prev_ts = ts

    parts = [_CONTEXT_FRAMING]
    parts.extend(context_lines)
    parts.append("---")
    return "\n".join(parts)


def build_prompt(message: str, context: list) -> str:
    """Build combined prompt string for runners that need it (e.g. codex)."""
    system_ctx = _build_system_context(context)
    if not system_ctx:
        return message
    return system_ctx + "\n\nJared's message: " + message


# ---------------------------------------------------------------------------
# Trace parsing helpers
# ---------------------------------------------------------------------------

def _parse_trace(raw_json: str) -> tuple[str, list, bool]:
    """
    Parse the JSON output from `claude -p --output-format json`.
    Returns (response_text, trace, has_tool_use).
    """
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return raw_json.strip(), [], False

    if isinstance(data, dict):
        return data.get("result", raw_json.strip()), [], False

    if isinstance(data, list):
        response = ""
        has_tool_use = False
        trace = []
        for item in data:
            typ = item.get("type", "")
            if typ == "result":
                response = item.get("result", "")
            elif typ == "assistant":
                content = item.get("content") or item.get("message", {}).get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if block.get("type") == "tool_use":
                            has_tool_use = True
                trace.append(item)
            elif typ == "user":
                trace.append(item)
        return response, trace, has_tool_use

    return raw_json.strip(), [], False


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------


def load_recent_summaries(channel=None):
    """Load session summaries for the last 7 days, filtered to channel."""
    now = datetime.now()
    start = now - timedelta(days=7)
    summaries = _mcp_load_summaries(start, now)
    summaries = [s for s in summaries
                 if s.get("summary") and s.get("summary") != "Summary unavailable."]
    if channel is None:
        return summaries
    filtered = []
    for s in summaries:
        if s.get("channel") == channel:
            filtered.append(s)
        elif channel in s.get("sources", []):
            filtered.append(s)
    return filtered


def _build_hybrid_context(summaries, recent_entries):
    """Build context from session summaries + most recent raw exchanges."""
    if not summaries and not recent_entries:
        return None

    parts = [_CONTEXT_FRAMING]
    total = 0

    if summaries:
        parts.append("Session summaries (oldest \u2192 newest):\n")
        prev_end = None
        for s in sorted(summaries, key=lambda x: x.get("start", "")):
            try:
                start_dt = datetime.fromisoformat(s["start"])
                end_dt = datetime.fromisoformat(s["end"])
            except (KeyError, ValueError):
                continue
            start_str = start_dt.strftime("%Y-%m-%d %H:%M")
            end_str = end_dt.strftime("%H:%M")
            if prev_end is not None:
                gap = (start_dt - prev_end).total_seconds()
                if gap >= _GAP_THRESHOLD_SECS:
                    parts.append("\n--- " + _format_gap(int(gap)) + " later ---\n")
            kw = ", ".join(s.get("keywords", [])[:8])
            line = "[" + start_str + " \u2192 " + end_str + "] " + s["summary"]
            if kw:
                line += "  [Keywords: " + kw + "]"
            parts.append(line)
            total += len(line)
            prev_end = end_dt
            if total > MAX_CONTEXT_CHARS * 2 // 3:
                break

    if recent_entries:
        parts.append("\n---\nMost recent exchanges:\n")
        prev_ts = None
        for entry in sorted(recent_entries, key=lambda e: e.get("timestamp", "")):
            ts = datetime.fromisoformat(entry["timestamp"])
            ts_str = ts.strftime("%Y-%m-%d %H:%M")
            user = entry.get("user", "")[:400]
            claude = entry.get("claude", "")[:400]
            if prev_ts is not None:
                gap_secs = (ts - prev_ts).total_seconds()
                if gap_secs >= _GAP_THRESHOLD_SECS:
                    parts.append("\n--- " + _format_gap(int(gap_secs)) + " later ---\n")
            parts.append("[" + ts_str + "] Jared: " + user + "\n[" + ts_str + "] Response: " + claude)
            prev_ts = ts

    parts.append("---")
    return "\n".join(parts)


_TELEGRAM_SYSTEM_INSTRUCTIONS = """

# Telegram Formatting Instructions

When responding via Telegram, use HTML formatting tags (the bot sends with parse_mode='HTML'):

Supported HTML tags:
- <b>bold text</b> or <strong>bold text</strong>
- <i>italic text</i> or <em>italic text</em>
- <u>underline</u>
- <s>strikethrough</s>
- <code>inline code</code>
- <pre>code blocks</pre>
- <blockquote>blockquotes</blockquote>
- <a href="url">links</a>

Important:
- Do NOT use Markdown syntax (`**bold**`, `_italic_`, etc.) — it will display as raw text
- Use HTML tags instead — they will render properly in the Telegram app
- Remember to escape special HTML characters in regular text: use &lt; for <, &gt; for >, &amp; for &
- Keep formatting clean and readable — use bold for emphasis, code tags for technical terms, etc.
"""


def run_claude(message: str, source: str = "unknown", model: str = "sonnet") -> str:
    """
    Run claude -p with recent history as context, save the exchange, return response.

    Args:
        message: The user's message.
        source:  Where it came from — "pi-telegram", "claude-http", etc.
        model:   Claude model — "sonnet", "opus", "haiku".
    """
    import time as _time
    t0 = _time.monotonic()

    channel = _source_channel(source)
    summaries = load_recent_summaries(channel=None if source in _ALL_CONTEXT_SOURCES else channel)
    if source in _ALL_CONTEXT_SOURCES:
        recent = load_recent_context(hours=2, channel=None)
    else:
        recent = load_recent_context(hours=2, channel=channel)
    recent = recent[-RECENT_RAW_COUNT:]
    system_ctx = _build_hybrid_context(summaries, recent)

    t_ctx = _time.monotonic()
    ctx_len = len(system_ctx) if system_ctx else 0
    log.info(f"Running claude (source={source}, channel={channel}, model={model}, summaries={len(summaries)}, recent_entries={len(recent)}, ctx_chars={ctx_len}, ctx_build={t_ctx - t0:.2f}s)")

    env = os.environ.copy()
    env["CLAUDE_SOURCE"] = source
    bot_token = env.get("BOT_TOKEN", "")
    if "telegram" in source and bot_token:
        log.info(f"BOT_TOKEN propagated to subprocess (token ends ...{bot_token[-6:]})")
    elif "telegram" in source:
        log.warning(f"BOT_TOKEN NOT found in environment for telegram source={source}")

    # Add Telegram formatting instructions if source is Telegram-based
    if "telegram" in source.lower():
        if system_ctx:
            system_ctx = system_ctx + _TELEGRAM_SYSTEM_INSTRUCTIONS
        else:
            system_ctx = _TELEGRAM_SYSTEM_INSTRUCTIONS.strip()

    cmd = ["claude", "-p", "--dangerously-skip-permissions", "--model", model,
           "--output-format", "json", "--verbose", "-"]
    if system_ctx:
        cmd = ["claude", "-p", "--dangerously-skip-permissions", "--model", model,
               "--append-system-prompt", system_ctx,
               "--output-format", "json", "--verbose", "-"]

    t_pre = _time.monotonic()
    result = subprocess.run(
        cmd,
        input=message,
        capture_output=True,
        text=True,
        cwd=str(HOME_DIR),
        env=env,
    )
    t_claude = _time.monotonic()
    log.info(f"Claude subprocess done (rc={result.returncode}, claude_time={t_claude - t_pre:.1f}s, total={t_claude - t0:.1f}s)")
    raw = (result.stdout or "").strip()
    if result.returncode != 0 and not raw:
        stderr = (result.stderr or "").strip()
        log.error(f"Claude subprocess failed (rc={result.returncode}): {stderr}")
        error_context = f"A previous Claude subprocess just failed with exit code {result.returncode}."
        if stderr:
            error_context += f" The stderr output was:\n{stderr[:2000]}"
        error_context += f"\n\nThe original user message was: {message}\n\nBriefly explain what went wrong and what the user can do."
        try:
            err_result = subprocess.run(
                ["claude", "-p", "--dangerously-skip-permissions"],
                input=error_context,
                capture_output=True,
                text=True,
                cwd=str(HOME_DIR),
                env=env,
            )
            response = (err_result.stdout or "").strip() or stderr or "Something went wrong."
        except Exception:
            response = stderr or "Something went wrong."
        append_exchange("system-error", message, response)
        return response

    response, trace, has_tool_use = _parse_trace(raw)
    response = response or "(No response)"
    append_exchange(source, message, response, trace=trace, has_tool_use=has_tool_use)
    return response
