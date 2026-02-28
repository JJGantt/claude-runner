#!/usr/bin/env python3
"""
Shared Codex runner for message pipelines (Telegram, HTTP).

Uses the local Codex CLI to process a prompt and returns the response.
Logs each exchange to history via mcp-history.
"""

import json
import os
import subprocess
import logging
from pathlib import Path

from config import HOME_DIR, CODEX_BIN
from runner import load_recent_context, build_prompt, append_exchange, _source_channel, _ALL_CONTEXT_SOURCES

log = logging.getLogger(__name__)

DEFAULT_EXEC_FLAGS = "--dangerously-bypass-approvals-and-sandbox --skip-git-repo-check"


def _resolve_codex_bin() -> str:
    env_bin = os.environ.get("CODEX_BIN")
    if env_bin:
        return env_bin
    if Path(CODEX_BIN).exists():
        return CODEX_BIN
    return "codex"


def _exec_flags() -> list[str]:
    raw = (os.environ.get("CODEX_EXEC_FLAGS") or "").strip()
    if raw:
        return raw.split()
    return DEFAULT_EXEC_FLAGS.split()


def _parse_codex_jsonl(raw: str) -> tuple[str, list, bool]:
    """
    Parse JSONL output from `codex exec --json`.
    Returns (response_text, trace, has_tool_use).
    """
    response = ""
    trace = []
    has_tool_use = False
    tool_item_types = {
        "function_call",
        "tool_call",
        "mcp_tool_call",
        "command_execution",
    }

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        typ = event.get("type", "")

        if typ == "item.completed":
            item = event.get("item", {})
            item_type = item.get("type", "")
            trace.append(item)
            if item_type == "agent_message":
                response = item.get("text", "")
            elif item_type in tool_item_types or (isinstance(item_type, str) and item_type.endswith("_call")):
                has_tool_use = True
        elif typ == "turn.completed":
            trace.append(event)

    return response, trace, has_tool_use


def run_codex(message: str, source: str = "unknown") -> str:
    """
    Run the local Codex CLI with recent history as context, save the exchange, return response.
    """
    channel = _source_channel(source)
    if source in _ALL_CONTEXT_SOURCES:
        context = load_recent_context(channel=None)
    else:
        context = load_recent_context(channel=channel)
    prompt = build_prompt(message, context)

    codex_bin = _resolve_codex_bin()
    flags = _exec_flags()

    log.info(f"Running codex (source={source}, channel={channel}, flags={' '.join(flags)}, context_entries={len(context)})")

    try:
        result = subprocess.run(
            [codex_bin, "exec", *flags, "--json", "-"],
            input=prompt,
            capture_output=True,
            text=True,
            cwd=str(HOME_DIR),
        )
        raw = (result.stdout or "").strip()
        if result.returncode != 0 and not raw:
            stderr = (result.stderr or "").strip()
            log.error(f"Codex subprocess failed (rc={result.returncode}): {stderr}")
            error_context = f"The Codex CLI subprocess just failed with exit code {result.returncode}."
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
                )
                response = (err_result.stdout or "").strip() or stderr or "Something went wrong."
            except Exception:
                response = stderr or "Something went wrong."
            append_exchange("system-error", message, response)
            return response

        response, trace, has_tool_use = _parse_codex_jsonl(raw)
        response = response or "(No response)"
    except FileNotFoundError:
        response = "Codex CLI not found on this Pi. Install it or set CODEX_BIN."
        trace = []
        has_tool_use = False

    append_exchange(source, message, response, trace=trace, has_tool_use=has_tool_use)
    return response
