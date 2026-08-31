from __future__ import annotations

import argparse
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from .config import DATA_DIR, LOG_DIR, STATE_DB, load_settings
from .gui import main as gui_main
from .storage import AgentStorage


def configure_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    handler = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)


def single_instance_lock():
    lock_path = DATA_DIR / "agent.lock"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    # ``a+b`` moves every write/lock operation to the end of the file. That
    # would let concurrent processes lock different bytes, so use a fixed
    # byte offset for the Windows CRT lock instead.
    handle = open(lock_path, "r+b")
    if os.name == "nt":
        import msvcrt
        try:
            handle.seek(0)
            if not handle.read(1):
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return None
    return handle


def smoke_test() -> int:
    settings = load_settings()
    storage = AgentStorage(STATE_DB)
    storage.set_meta("last_smoke_test", "ok")
    storage.close()
    print(f"{settings['device_id']} {STATE_DB}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Hamshmareh Desktop Agent")
    parser.add_argument("--smoke-test", action="store_true", help="initialize local state and exit")
    args = parser.parse_args()
    lock = single_instance_lock()
    if lock is None:
        print("Hamshmareh Extractor is already running.", file=sys.stderr)
        return 2
    configure_logging()
    if args.smoke_test:
        return smoke_test()
    try:
        return gui_main()
    finally:
        lock.close()


if __name__ == "__main__":
    raise SystemExit(main())
