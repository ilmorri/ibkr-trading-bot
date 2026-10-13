from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from .logger import get_logger

if TYPE_CHECKING:
    from .execution import ExecutionEngine

logger = get_logger("state")


def default_state_path() -> Path:
    # src/bot/state.py -> trading-bot/data/state.json
    return Path(__file__).resolve().parents[2] / "data" / "state.json"


class StateStore:
    """
    Saves the risk manager's counters and the execution engine's open trades
    to one JSON file, so a restart does not forget today's trades or an open
    position. Limits are not saved: they always come from the config.
    """

    VERSION = 1

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else default_state_path()

    def save(self, execution: "ExecutionEngine") -> None:
        data = {
            "version": self.VERSION,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "risk": execution.risk.state_dict(),
            "execution": execution.state_dict(),
        }

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")

        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

        # Atomic on the same filesystem: a crash leaves the old file or the new one, never half of each.
        os.replace(tmp, self.path)

    def load(self, execution: "ExecutionEngine") -> bool:
        """Restore saved state into `execution` and its risk manager. False if there is none."""
        if not self.path.exists():
            logger.info("state_none path=%s", self.path)
            return False

        with self.path.open("r", encoding="utf-8") as f:
            data = json.load(f)

        if data.get("version") != self.VERSION:
            raise ValueError(f"Unsupported state version {data.get('version')} in {self.path}")

        execution.risk.load_state(data.get("risk", {}))
        execution.load_state(data.get("execution", {}))

        logger.info(
            "state_loaded path=%s saved_at=%s open_trades=%d pending_orders=%d",
            self.path,
            data.get("saved_at"),
            len(execution.open_trades),
            len(execution.pending_orders),
        )
        return True

    def attach(self, execution: "ExecutionEngine") -> None:
        """Save automatically after every change the execution engine makes."""
        execution.on_change = self.save
