from __future__ import annotations

import logging
from pathlib import Path

_CONFIGURED = False


def _project_root() -> Path:
    # src/bot/logger.py
    # parents[0] = bot
    # parents[1] = src
    # parents[2] = trading-bot
    return Path(__file__).resolve().parents[2]


def setup_logging() -> None:
    global _CONFIGURED

    if _CONFIGURED:
        return

    log_dir = _project_root() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / "bot.log"

    handlers: list[logging.Handler] = [logging.StreamHandler()]

    try:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except Exception:
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        handlers=handlers,
        force=True,
    )

    logging.getLogger("ib_insync").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup_logging()
    return logging.getLogger(name)
