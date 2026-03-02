# claude-runner

Atomic package for Claude/Codex CLI execution logic. The single source of truth imported by both [telegram-bot](https://github.com/JJGantt/telegram-bot) and [pi-server](https://github.com/JJGantt/pi-server).

## Overview

**claude-runner** encapsulates:
- Claude/Codex CLI invocation with configurable parameters
- Context injection (history, memory, system prompts)
- History I/O and logging
- Mode/model state management (persist whether you're in "claude" or "codex" mode, and which model)

No business logic or routing lives here — just execution infrastructure.

## Modules

| Module | Purpose |
|--------|---------|
| `runner.py` | Claude CLI execution + context injection + history I/O |
| `codex_runner.py` | Codex CLI execution (similar API to runner.py) |
| `config.py` | Singleton config loader (reads config.json) |
| `route_state.py` | Mode/model state management (claude vs codex, sonnet/opus/haiku) |

## Configuration

All paths and settings in `config.json` (no hardcoded values):

```json
{
  "claude_path": "/path/to/claude",
  "history_dir": "/home/jaredgantt/data/history",
  "context_dir": "/home/jaredgantt/data/context",
  "mode_state_file": "/tmp/route_state.json"
}
```

## Design Decisions

- **No timeouts** — Subprocess calls run as long as needed
- **History I/O delegated** — Logic is in [mcp-history](https://github.com/JJGantt/mcp-history)
- **All paths external** — Pulled from config.json, no hardcoding

## Usage

```python
from runner import run_claude

response = run_claude(
    text="What's the weather?",
    source="telegram-bot",
    inject_history=True,
    context_hours=24
)
```

## Related

- **Bot:** [telegram-bot](https://github.com/JJGantt/telegram-bot) — Telegram interface
- **Server:** [pi-server](https://github.com/JJGantt/pi-server) — HTTP interface
- **History:** [mcp-history](https://github.com/JJGantt/mcp-history) — Context & logging
