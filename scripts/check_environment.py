#!/usr/bin/env python3

import platform
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> int:
    print("=" * 60)
    print("Trading bot environment check")
    print("=" * 60)

    print(f"Project root: {ROOT}")
    print(f"Python: {platform.python_version()}")
    print(f"Platform: {platform.platform()}")

    required_modules = ["ib_insync", "pandas", "numpy", "yaml"]

    for name in required_modules:
        try:
            module = __import__(name)
            version = getattr(module, "__version__", "installed")
            print(f"OK   {name}: {version}")
        except Exception as exc:
            print(f"FAIL {name}: {exc}")
            return 1

    try:
        from bot.config import load_config
    except Exception as exc:
        print(f"FAIL config loader import: {exc}")
        return 1

    try:
        cfg = load_config()
    except Exception as exc:
        print(f"FAIL config load: {exc}")
        return 1

    host = cfg["ibkr"]["host"]
    port = int(cfg["ibkr"]["port"])
    client_id = cfg["ibkr"]["clientId"]

    print(f"IBKR host: {host}")
    print(f"IBKR port: {port}")
    print(f"IBKR clientId: {client_id}")
    print(f"Exchange: {cfg['market']['exchange']}")
    print(f"Currency: {cfg['market']['currency']}")
    print(f"Universe: {[item['symbol'] for item in cfg['universe']]}")

    print("-" * 60)

    try:
        sock = socket.create_connection((host, port), timeout=2)
        sock.close()
        print(f"NETWORK OK: {host}:{port} is reachable")
    except Exception as exc:
        print(f"NETWORK WARNING: {host}:{port} is not reachable: {exc}")
        print("This is OK if IB Gateway is not running yet.")

    print("-" * 60)
    print("Environment check complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
