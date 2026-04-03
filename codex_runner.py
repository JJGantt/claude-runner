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
from datetime import datetime, timedelta
from pathlib import Path

from config import DATA_DIR, HOME_DIR, CODEX_BIN
from runner import load_recent_context, build_prompt, append_exchange, _source_channel, _ALL_CONTEXT_SOURCES

log = logging.getLogger(__name__)

DEFAULT_EXEC_FLAGS = "--dangerously-bypass-approvals-and-sandbox --skip-git-repo-check"
CODEX_SESSION_FILE = DATA_DIR / "codex_sessions.json"
CODEX_TELEGRAM_ROLLOVER_HOUR = 5
CODEX_TELEGRAM_FORMATTING_PROMPT = """You are responding to Jared in Telegram.

Format the final answer for a Telegram chat message:
- Prefer plain text, short bullets, and label-value lines.
- Do not use Markdown tables.
- Do not use ASCII or aligned text tables.
- Do not use code blocks in normal replies.
- If data is naturally table-shaped, rewrite it into concise bullets or label-value lines.
- Preserve all important information; only change the presentation.
- Keep the response compact and easy to scan on a phone.
"""


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


def _parse_codex_jsonl(raw: str) -> tuple[str, list, bool, str | None]:
    """
    Parse JSONL output from `codex exec --json`.
    Returns (response_text, trace, has_tool_use, thread_id).
    """
    response = ""
    trace = []
    has_tool_use = False
    thread_id = None
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

        if typ == "thread.started":
            thread_id = event.get("thread_id")
        elif typ == "item.completed":
            item = event.get("item", {})
            item_type = item.get("type", "")
            trace.append(item)
            if item_type == "agent_message":
                response = item.get("text", "")
            elif item_type in tool_item_types or (isinstance(item_type, str) and item_type.endswith("_call")):
                has_tool_use = True
        elif typ == "turn.completed":
            trace.append(event)

    return response, trace, has_tool_use, thread_id


def _load_codex_sessions() -> dict:
    try:
        data = json.loads(CODEX_SESSION_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_codex_sessions(data: dict):
    CODEX_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CODEX_SESSION_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(CODEX_SESSION_FILE)


def _codex_day_bucket(now: datetime | None = None) -> str:
    current = now or datetime.now().astimezone()
    if current.hour < CODEX_TELEGRAM_ROLLOVER_HOUR:
        current -= timedelta(days=1)
    return current.date().isoformat()


def _load_daily_thread(source: str) -> tuple[str | None, str | None]:
    entry = _load_codex_sessions().get(source)
    if not isinstance(entry, dict):
        return None, None
    bucket = str(entry.get("bucket", "")).strip() or None
    thread_id = str(entry.get("thread_id", "")).strip() or None
    return bucket, thread_id


def _save_daily_thread(source: str, bucket: str, thread_id: str):
    data = _load_codex_sessions()
    data[source] = {"bucket": bucket, "thread_id": thread_id}
    _save_codex_sessions(data)


def _clear_daily_thread(source: str):
    data = _load_codex_sessions()
    if source in data:
        del data[source]
        _save_codex_sessions(data)


def _build_codex_prompt(message: str, source: str) -> tuple[str, int]:
    if source == "codex-telegram":
        prompt = (
            f"{CODEX_TELEGRAM_FORMATTING_PROMPT}\n"
            f"Jared's message:\n{message}"
        )
        return prompt, 0

    channel = _source_channel(source)
    if source in _ALL_CONTEXT_SOURCES:
        context = load_recent_context(channel=None)
    else:
        context = load_recent_context(channel=channel)
    return build_prompt(message, context), len(context)


def _run_codex_subprocess(codex_bin: str, flags: list[str], prompt: str, thread_id: str | None):
    if thread_id:
        cmd = [codex_bin, "exec", "resume", thread_id, *flags, "--json", "-"]
    else:
        cmd = [codex_bin, "exec", *flags, "--json", "-"]
    return subprocess.run(
        cmd,
        input=prompt,
        capture_output=True,
        text=True,
        cwd=str(HOME_DIR),
    )


def run_codex(message: str, source: str = "unknown") -> str:
    """
    Run the local Codex CLI, save the exchange, return response.
    """
    codex_bin = _resolve_codex_bin()
    flags = _exec_flags()
    prompt, context_entries = _build_codex_prompt(message, source)
    bucket = _codex_day_bucket() if source == "codex-telegram" else None
    saved_bucket, saved_thread_id = _load_daily_thread(source) if bucket else (None, None)
    resume_thread_id = saved_thread_id if saved_bucket == bucket else None

    log.info(
        f"Running codex (source={source}, flags={' '.join(flags)}, context_entries={context_entries}, "
        f"bucket={bucket or '-'}, resume={'yes' if resume_thread_id else 'no'}"
        f"{', tid=' + resume_thread_id[:8] if resume_thread_id else ''})"
    )

    try:
        result = _run_codex_subprocess(codex_bin, flags, prompt, resume_thread_id)
        raw = (result.stdout or "").strip()
        if result.returncode != 0 and resume_thread_id:
            stderr = (result.stderr or "").strip()
            log.warning(
                f"Codex resume failed (source={source}, bucket={bucket}, tid={resume_thread_id[:8]}): "
                f"{stderr[:200]}"
            )
            _clear_daily_thread(source)
            result = _run_codex_subprocess(codex_bin, flags, prompt, None)
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
                    ["claude", "-p", "--permission-mode", "bypassPermissions"],
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

        response, trace, has_tool_use, thread_id = _parse_codex_jsonl(raw)
        response = response or "(No response)"
    except FileNotFoundError:
        response = "Codex CLI not found on this Pi. Install it or set CODEX_BIN."
        trace = []
        has_tool_use = False
        thread_id = None

    effective_thread_id = thread_id or resume_thread_id
    if bucket and effective_thread_id:
        _save_daily_thread(source, bucket, effective_thread_id)

    append_exchange(
        source,
        message,
        response,
        session_id=effective_thread_id,
        trace=trace,
        has_tool_use=has_tool_use,
    )
    return response
