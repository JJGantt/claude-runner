#!/usr/bin/env python3
"""
Shared message runner for all pipelines (Telegram bot, HTTP server, etc.).

HISTORY SYSTEM
==============
Every exchange is saved to a per-day JSON file via mcp-history.

SESSION CONTINUITY
==================
Model-specific Telegram bots (opus, sonnet, haiku, codex) use --resume
with stored session IDs for lossless conversation continuity.

The pi-telegram bot runs fresh each message — no context injection, no resume.
It has MCP history tools available for on-demand context retrieval.

Channel mapping (for history storage):
    sonnet-telegram   →  "sonnet-telegram" channel
    opus-telegram     →  "opus-telegram" channel
    haiku-telegram    →  "haiku-telegram" channel
    codex-telegram    →  "codex-telegram" channel
    pi-telegram       →  "pi-telegram" channel
    claude-http, codex-http  →  "http" channel
    claude-mac, codex-mac, claude-pi, codex-pi  →  "mac" channel
    claude-voice      →  "voice" channel
    claude-ambient    →  "ambient" channel
"""

import json
import os
import subprocess
import logging
import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

from config import MCP_HISTORY_DIR, HOME_DIR, DATA_DIR

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

log = logging.getLogger(__name__)

# Map each source to its channel for history storage.
_SOURCE_TO_CHANNEL = {
    "sonnet-telegram":  "sonnet-telegram",
    "opus-telegram":    "opus-telegram",
    "haiku-telegram":   "haiku-telegram",
    "codex-telegram":   "codex-telegram",
    "pi-telegram":      "pi-telegram",
    "claude-http":      "http",
    "codex-http":       "http",
    "claude-mac":       "mac",
    "codex-mac":        "mac",
    "claude-pi":        "mac",
    "codex-pi":         "mac",
    "claude-voice":     "voice",
    "claude-ambient":   "ambient",
    # Legacy
    "claude-telegram":  "telegram",
    "telegram":         "telegram",
    "http":             "http",
    "laptop":           "mac",
    "interactive":      "pi",
}

# Sources that use --resume for session continuity
_RESUME_SOURCES = {
    "opus-telegram",
    "sonnet-telegram",
    "haiku-telegram",
    "codex-telegram",
    "pi-telegram",
}

# Sources that receive full cross-channel context (used by codex_runner)
_ALL_CONTEXT_SOURCES = {"pi-telegram"}

# ---------------------------------------------------------------------------
# Session ID persistence
# ---------------------------------------------------------------------------

_SESSIONS_FILE = DATA_DIR / "sessions.json"


def _load_sessions() -> dict:
    """Load stored session IDs from disk."""
    try:
        return json.loads(_SESSIONS_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_session(source: str, session_id: str):
    """Save a session ID for a source."""
    sessions = _load_sessions()
    sessions[source] = session_id
    _SESSIONS_FILE.write_text(json.dumps(sessions, indent=2) + "\n")


def _get_session_id(source: str) -> str | None:
    """Get stored session ID for a source, or None."""
    return _load_sessions().get(source)


def _clear_session(source: str):
    """Remove a stored session ID (e.g. after resume failure)."""
    sessions = _load_sessions()
    sessions.pop(source, None)
    _SESSIONS_FILE.write_text(json.dumps(sessions, indent=2) + "\n")


# ---------------------------------------------------------------------------
# History I/O — delegated to mcp-history
# ---------------------------------------------------------------------------

def append_exchange(source: str, user_msg: str, claude_response: str,
                    session_id: str | None = None,
                    trace: list | None = None, has_tool_use: bool = False):
    """Append a single exchange to today's history file via mcp-history."""
    extra = {"has_tool_use": has_tool_use}
    if session_id:
        extra["session_id"] = session_id
    if trace is not None:
        extra["trace"] = trace
    _mcp_append_entry(source, user_msg, claude_response, **extra)


def load_history_range(start: datetime, end: datetime) -> list:
    """Return all exchanges between start and end (inclusive by day)."""
    return _mcp_load_range(start, end)


def _source_channel(source: str) -> str | None:
    """Return the channel for a given source, or None."""
    ch = _SOURCE_TO_CHANNEL.get(source)
    if ch is not None:
        return ch
    if source.startswith("claude-ambient-"):
        return "ambient"
    return None


# ---------------------------------------------------------------------------
# Prompt building (legacy — kept for codex_runner and HTTP)
# ---------------------------------------------------------------------------

def load_recent_context(hours: int = 24, channel: str | None = None) -> list:
    """Return exchanges from the last N hours for context injection."""
    now = datetime.now()
    raw = load_history_range(now - timedelta(hours=hours), now)
    cleaned = []
    for entry in raw:
        src = entry.get("source", "")
        entry_channel = _SOURCE_TO_CHANNEL.get(src)
        if entry_channel is None:
            continue
        if channel is not None and entry_channel != channel:
            continue
        user = entry.get("user", "").lstrip(" -\n")
        if user.startswith("Recent conversation history") or \
           user.startswith("A previous Claude subprocess") or \
           user.startswith("The Codex CLI subprocess") or \
           user.startswith("You are responding to"):
            continue
        cleaned.append(entry)
    return cleaned


def build_prompt(message: str, context: list) -> str:
    """Build combined prompt string for runners that need it (e.g. codex)."""
    if not context:
        return message
    lines = []
    for entry in sorted(context, key=lambda e: e.get("timestamp", "")):
        ts = entry.get("timestamp", "")[:16]
        user = entry.get("user", "")[:450]
        claude = entry.get("claude", "")[:450]
        lines.append(f"[{ts}] Jared: {user}\n[{ts}] Response: {claude}")
    ctx = "\n".join(lines)
    return f"Recent conversation context:\n{ctx}\n\nJared's message: {message}"


# ---------------------------------------------------------------------------
# Trace parsing helpers
# ---------------------------------------------------------------------------

def _parse_trace(raw_json: str) -> tuple[str, list, bool, str | None]:
    """
    Parse the JSON output from `claude -p --output-format json`.
    Returns (response_text, trace, has_tool_use, session_id).
    """
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return raw_json.strip(), [], False, None

    if isinstance(data, dict):
        return (data.get("result", raw_json.strip()), [], False,
                data.get("session_id"))

    if isinstance(data, list):
        response = ""
        has_tool_use = False
        session_id = None
        trace = []
        for item in data:
            typ = item.get("type", "")
            if item.get("session_id"):
                session_id = item["session_id"]
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
        return response, trace, has_tool_use, session_id

    return raw_json.strip(), [], False, None


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

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

_PI_BOT_SYSTEM = """You are responding to Jared via Telegram on his Raspberry Pi 5. You are the general-purpose Pi bot with full system diagnostic capabilities. Each message is a fresh session — you do NOT have prior conversation context injected.

You have two categories of MCP tools:

HISTORY TOOLS — for conversation context:
  get_summaries, search_history, get_session, get_response, get_trace
  If Jared references earlier messages, use these to look up context.
  Do not claim you lack memory — you have the tools to retrieve it.

SYSTEM DIAGNOSTIC TOOLS (pi-ops) — for Pi health and troubleshooting:
  get_system_status — full health report (memory, swap, disk, temp, services, ports, logs, sync)
  get_top_processes — top N processes by memory or CPU
  get_service_logs — recent journalctl for a monitored service
  get_alert_history — current active alerts and how long they have been firing
  get_service_list — all monitored services with status and uptime

DIAGNOSTIC DECISION TREE:
  "Why is swap/memory high?" -> get_system_status -> get_top_processes(sort_by=memory) -> get_service_logs if a service is the culprit
  "What is wrong?" / "System status?" -> get_system_status -> follow up on any CRIT/WARN items
  "Why did X crash?" / "X is down" -> get_service_logs(service=X) -> get_alert_history for duration
  "Is everything running?" -> get_service_list
  Always start with get_system_status for open-ended questions, then drill down.

Monitored services: claude-bots, pi-server, tv-server, mcp-history-receiver, mcp-history-watch.
"""


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------


def run_claude(message: str, source: str = "unknown", model: str = "sonnet") -> str:
    """
    Run claude -p, save the exchange, return response.

    For resume-enabled sources (opus/sonnet/haiku/codex-telegram):
        Uses --resume with stored session ID for lossless continuity.
        Falls back to fresh session if resume fails.

    For other sources (pi-telegram, http, etc.):
        Fresh session each time with minimal system prompt.
    """
    import time as _time
    t0 = _time.monotonic()

    channel = _source_channel(source)
    use_resume = source in _RESUME_SOURCES
    session_id = _get_session_id(source) if use_resume else None

    # Build system prompt
    system_parts = []
    if source == "pi-telegram":
        system_parts.append(_PI_BOT_SYSTEM)
    if "telegram" in source.lower():
        system_parts.append(_TELEGRAM_SYSTEM_INSTRUCTIONS)
    system_ctx = "\n".join(system_parts).strip() or None

    log.info(f"Running claude (source={source}, channel={channel}, model={model}, "
             f"resume={'yes' if session_id else 'no'}{', sid=' + session_id[:8] if session_id else ''})")

    env = os.environ.copy()
    env["CLAUDE_SOURCE"] = source
    bot_token = env.get("BOT_TOKEN", "")
    if "telegram" in source and bot_token:
        log.info(f"BOT_TOKEN propagated to subprocess (token ends ...{bot_token[-6:]})")
    elif "telegram" in source:
        log.warning(f"BOT_TOKEN NOT found in environment for telegram source={source}")

    # Build command
    cmd = ["claude", "-p", "--permission-mode", "bypassPermissions", "--model", model,
           "--output-format", "json", "--verbose"]
    if session_id:
        cmd.extend(["--resume", session_id])
    if system_ctx:
        cmd.extend(["--append-system-prompt", system_ctx])
    cmd.append("-")

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
    log.info(f"Claude subprocess done (rc={result.returncode}, "
             f"claude_time={t_claude - t_pre:.1f}s, total={t_claude - t0:.1f}s)")

    raw = (result.stdout or "").strip()

    # If resume failed, retry without it
    if result.returncode != 0 and session_id:
        stderr = (result.stderr or "").strip()
        log.warning(f"Resume failed (rc={result.returncode}), retrying fresh: {stderr[:200]}")
        _clear_session(source)

        cmd_retry = ["claude", "-p", "--permission-mode", "bypassPermissions", "--model", model,
                     "--output-format", "json", "--verbose"]
        if system_ctx:
            cmd_retry.extend(["--append-system-prompt", system_ctx])
        cmd_retry.append("-")

        result = subprocess.run(
            cmd_retry,
            input=message,
            capture_output=True,
            text=True,
            cwd=str(HOME_DIR),
            env=env,
        )
        raw = (result.stdout or "").strip()
        t_claude = _time.monotonic()
        log.info(f"Retry done (rc={result.returncode}, total={t_claude - t0:.1f}s)")

    if result.returncode != 0 and not raw:
        stderr = (result.stderr or "").strip()
        log.error(f"Claude subprocess failed (rc={result.returncode}): {stderr}")
        error_context = f"A previous Claude subprocess just failed with exit code {result.returncode}."
        if stderr:
            error_context += f" The stderr output was:\n{stderr[:2000]}"
        error_context += f"\n\nThe original user message was: {message}\n\nBriefly explain what went wrong and what the user can do."
        try:
            err_result = subprocess.run(
                ["claude", "-p", "--permission-mode", "bypassPermissions"],
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

    response, trace, has_tool_use, new_session_id = _parse_trace(raw)
    response = response or "(No response)"

    # Store session ID for resume-enabled sources
    if use_resume and new_session_id:
        _save_session(source, new_session_id)

    append_exchange(source, message, response, session_id=new_session_id, trace=trace, has_tool_use=has_tool_use)
    return response
