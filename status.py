from __future__ import annotations

import json
import pathlib
import sqlite3
import subprocess
import sys
from datetime import datetime


ROOT = pathlib.Path(__file__).resolve().parent
OUTPUT = ROOT / "output"


def running_bot_pids() -> list[str]:
    """Return PIDs of this scraper only; ignore unrelated Python processes."""
    command = (
        "Get-CimInstance Win32_Process -Filter \"Name = 'python.exe'\" "
        "| Where-Object { $_.CommandLine -match 'irancell_number_bot\\.py' } "
        "| Select-Object -ExpandProperty ProcessId"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip().isdigit()]


def read_status() -> dict:
    products = []
    total = 0

    for db_path in sorted(OUTPUT.glob("*/state.sqlite3")):
        with sqlite3.connect(db_path) as con:
            count = con.execute("SELECT COUNT(*) FROM numbers").fetchone()[0]
            done = con.execute(
                "SELECT COUNT(*) FROM patterns WHERE status = 'done'"
            ).fetchone()[0]
            pending = con.execute(
                "SELECT COUNT(*) FROM patterns WHERE status = 'pending'"
            ).fetchone()[0]

        product_id = db_path.parent.name.split("_", 1)[0]
        products.append(
            {
                "product_id": product_id,
                "folder": db_path.parent.name,
                "unique_numbers": count,
                "patterns_done": done,
                "patterns_pending": pending,
            }
        )
        total += count

    return {
        "running_pids": running_bot_pids(),
        "total_numbers_across_products": total,
        "products": products,
    }


def fmt(value: int) -> str:
    return f"{value:,}"


def main() -> None:
    # CMD on some Windows installations defaults to a legacy code page.
    # Keep the status command usable there when Persian text is printed.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    status = read_status()
    pids = status["running_pids"]

    print("=" * 58)
    print("وضعیت ربات استخراج شماره ایرانسل")
    print(f"زمان بررسی: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if pids:
        print(f"وضعیت ربات: فعال  | PID: {', '.join(pids)}")
    else:
        print("وضعیت ربات: متوقف یا قابل‌تشخیص نیست")
    print(f"مجموع شماره‌های ذخیره‌شده در بخش‌ها: {fmt(status['total_numbers_across_products'])}")
    print("نکته: این مجموع بین محصولات جداست؛ ممکن است یک شماره در دو محصول تکرار شود.")
    print("-" * 58)

    if not status["products"]:
        print("هنوز state.sqlite3 برای هیچ محصولی پیدا نشد.")
    else:
        for product in status["products"]:
            print(
                f"محصول {product['product_id']}: "
                f"{fmt(product['unique_numbers'])} شماره یکتا | "
                f"الگوهای تکمیل‌شده: {fmt(product['patterns_done'])} | "
                f"در صف: {fmt(product['patterns_pending'])}"
            )
    print("=" * 58)


if __name__ == "__main__":
    main()
