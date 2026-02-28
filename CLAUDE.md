# claude-runner

Atomic package for Claude/Codex CLI execution logic.
Single source of truth — imported by telegram-bot/ and pi-server/.

## Modules
- config.py — singleton config loader (config.json)
- runner.py — Claude CLI execution + context injection + history I/O
- codex_runner.py — Codex CLI execution
- route_state.py — mode/model state management (claude vs codex, sonnet/opus/haiku)

## Design decisions
- No timeouts on subprocess calls — let processes run as long as needed
- History I/O delegated to mcp-history (~/mcp-history/history_io.py)
- All paths from config.json — no hardcoded paths
