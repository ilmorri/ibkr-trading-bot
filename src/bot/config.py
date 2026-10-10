from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml


def _project_root() -> Path:
    # This file is: trading-bot/src/bot/config.py
    # parents[0] = bot
    # parents[1] = src
    # parents[2] = trading-bot
    return Path(__file__).resolve().parents[2]


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    if path is None:
        path = _project_root() / "config" / "config.yaml"

    path = Path(path)

    with path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    cfg.setdefault("ibkr", {})

    cfg["ibkr"]["host"] = os.environ.get("IB_HOST", cfg["ibkr"].get("host", "127.0.0.1"))
    cfg["ibkr"]["port"] = int(os.environ.get("IB_PORT", cfg["ibkr"].get("port", 7497)))
    cfg["ibkr"]["clientId"] = int(os.environ.get("IB_CLIENT_ID", cfg["ibkr"].get("clientId", 1)))

    cfg.setdefault("market", {})
    cfg["market"].setdefault("exchange", "MIL")
    cfg["market"].setdefault("currency", "EUR")
    cfg["market"].setdefault("timezone", "Europe/Rome")

    cfg.setdefault("universe", [])
    cfg.setdefault("risk", {})
    cfg.setdefault("trading", {})

    return cfg
