from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser

from dashboard import running_bot_pids


ROOT = pathlib.Path(__file__).resolve().parent
OUTPUT = ROOT / "output"
BOT = ROOT / "irancell_number_bot.py"
DASHBOARD = ROOT / "dashboard.py"
URL = "http://127.0.0.1:8765/"


def dashboard_is_running() -> bool:
    try:
        with urllib.request.urlopen(URL + "health", timeout=2) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    started_bot = False

    if not running_bot_pids():
        stdout_path = OUTPUT / "process.stdout.log"
        stderr_path = OUTPUT / "process.stderr.log"
        stdout_handle = stdout_path.open("a", encoding="utf-8", buffering=1)
        stderr_handle = stderr_path.open("a", encoding="utf-8", buffering=1)
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    "-u",
                    str(BOT),
                    "--products",
                    "all",
                    "--delay",
                    "8",
                    "--max-retries",
                    "10",
                    "--retry-forever-429",
                    "--rate-limit-cooldown",
                    "600",
                    "--output",
                    str(OUTPUT),
                ],
                cwd=ROOT,
                stdout=stdout_handle,
                stderr=stderr_handle,
                creationflags=flags,
            )
            started_bot = True
        finally:
            stdout_handle.close()
            stderr_handle.close()

    if not dashboard_is_running():
        subprocess.Popen(
            [sys.executable, str(DASHBOARD), "--output", str(OUTPUT), "--open-browser"],
            cwd=ROOT,
            creationflags=flags,
        )
    else:
        webbrowser.open(URL)

    print("ربات و داشبورد آماده شدند.")
    print("داشبورد: " + URL)
    print("ربات جدید اجرا شد: " + ("بله" if started_bot else "خیر؛ اجرای قبلی فعال بود"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
