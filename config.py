"""
Configuration loader for claude-runner.

Loads config.json from the package directory and provides a singleton CONFIG dict.
Mirrors the mcp-history config pattern.
"""

import json
from pathlib import Path

_CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"No config.json found at {_CONFIG_PATH}. "
            f"Copy config.example.json to config.json and fill in your values."
        )
    with open(_CONFIG_PATH) as f:
        return json.load(f)


CONFIG = load_config()

# Derived paths
DATA_DIR = Path(CONFIG["data_dir"])
MCP_HISTORY_DIR = Path(CONFIG["mcp_history_dir"])
HOME_DIR = Path(CONFIG["home_dir"])
MODE_FILE = Path(CONFIG["mode_file"])
CODEX_BIN = CONFIG.get("codex_bin", "codex")
MAX_CONTEXT_CHARS = CONFIG.get("max_context_chars", 12000)
CONTEXT_HOURS = CONFIG.get("context_hours", 24)
