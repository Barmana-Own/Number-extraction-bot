from __future__ import annotations

import argparse
import json
import pathlib
import re
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit


ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_OUTPUT = ROOT / "output"


INDEX_HTML = r"""<!doctype html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>مانیتور استخراج شماره ایرانسل</title>
  <style>
    :root { color-scheme: light; --ink:#172033; --muted:#64748b; --line:#e2e8f0; --card:#fff; --bg:#f4f7fb; --blue:#2563eb; --green:#15803d; --amber:#b45309; --red:#b91c1c; }
    * { box-sizing:border-box; }
    body { margin:0; background:var(--bg); color:var(--ink); font-family:Tahoma, "Segoe UI", sans-serif; }
    .wrap { max-width:1280px; margin:0 auto; padding:24px; }
    header { display:flex; align-items:center; justify-content:space-between; gap:16px; margin-bottom:20px; }
    h1 { margin:0 0 7px; font-size:25px; }
    h2 { margin:0 0 14px; font-size:18px; }
    p { margin:0; }
    .sub { color:var(--muted); font-size:13px; }
    .badge { border-radius:999px; padding:8px 13px; font-size:13px; font-weight:700; white-space:nowrap; background:#e2e8f0; color:#334155; }
    .badge.running { background:#dcfce7; color:var(--green); }
    .badge.offline { background:#fee2e2; color:var(--red); }
    .grid { display:grid; grid-template-columns:repeat(4, minmax(0, 1fr)); gap:14px; margin-bottom:18px; }
    .card, .panel { background:var(--card); border:1px solid var(--line); border-radius:16px; box-shadow:0 4px 18px rgba(15,23,42,.04); }
    .card { padding:17px 18px; }
    .label { color:var(--muted); font-size:12px; margin-bottom:8px; }
    .value { font-size:27px; font-weight:800; direction:ltr; text-align:right; }
    .panel { padding:20px; margin-bottom:18px; }
    .current { display:grid; grid-template-columns:1fr 1fr 1fr; gap:18px; }
    .field { min-width:0; }
    .field .value { font-size:18px; overflow-wrap:anywhere; }
    code { direction:ltr; unicode-bidi:plaintext; display:inline-block; padding:5px 8px; border-radius:7px; background:#f1f5f9; color:#0f172a; font-family:Consolas, monospace; font-size:14px; }
    .table-wrap { overflow-x:auto; }
    table { width:100%; border-collapse:collapse; min-width:870px; }
    th, td { padding:12px 10px; border-bottom:1px solid var(--line); text-align:right; vertical-align:top; font-size:13px; }
    th { color:var(--muted); font-size:12px; font-weight:700; background:#f8fafc; }
    tr:last-child td { border-bottom:0; }
    .status { display:inline-flex; border-radius:999px; padding:5px 9px; font-size:11px; font-weight:700; white-space:nowrap; }
    .s-running { color:var(--green); background:#dcfce7; }
    .s-complete { color:#1d4ed8; background:#dbeafe; }
    .s-queued, .s-waiting { color:var(--amber); background:#fef3c7; }
    .s-paused, .s-skipped { color:#475569; background:#e2e8f0; }
    .s-error { color:var(--red); background:#fee2e2; }
    .muted { color:var(--muted); }
    .mono { direction:ltr; text-align:left; font-family:Consolas, monospace; font-size:12px; }
    .log { margin:0; max-height:270px; overflow:auto; white-space:pre-wrap; direction:ltr; text-align:left; font:12px/1.8 Consolas, monospace; background:#0f172a; color:#dbeafe; border-radius:10px; padding:14px; }
    .notice { color:var(--muted); font-size:12px; line-height:1.8; }
    .error { color:var(--red); }
    @media (max-width:850px) { .grid { grid-template-columns:repeat(2, minmax(0, 1fr)); } .current { grid-template-columns:1fr; gap:12px; } header { align-items:flex-start; flex-direction:column; } }
    @media (max-width:500px) { .wrap { padding:14px; } .grid { grid-template-columns:1fr 1fr; gap:9px; } .value { font-size:21px; } }
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <div>
        <h1>مانیتور استخراج شماره ایرانسل</h1>
        <p class="sub">به‌روزرسانی خودکار هر ۳ ثانیه · فقط خواندنی · اطلاعات از state و لاگ محلی</p>
      </div>
      <div id="bot-badge" class="badge">در حال بررسی…</div>
    </header>

    <section class="grid">
      <div class="card"><div class="label">مجموع شماره‌های یکتا</div><div id="total-numbers" class="value">—</div></div>
      <div class="card"><div class="label">الگوهای باقی‌مانده</div><div id="total-pending" class="value">—</div></div>
      <div class="card"><div class="label">محصولات کامل‌شده</div><div id="complete-products" class="value">—</div></div>
      <div class="card"><div class="label">آخرین به‌روزرسانی داشبورد</div><div id="updated-at" class="value" style="font-size:16px">—</div></div>
    </section>

    <section class="panel">
      <h2 id="current-heading">استخراج فعلی</h2>
      <div id="current-empty" class="notice">در حال حاضر الگوی فعالی ثبت نشده است.</div>
      <div id="current-fields" class="current" hidden>
        <div class="field"><div class="label">محصول</div><div id="current-product" class="value">—</div></div>
        <div class="field"><div class="label">پیش‌شماره</div><div id="current-prefix" class="value"><code>—</code></div></div>
        <div class="field"><div class="label">خط / الگوی در حال بررسی</div><div id="current-pattern" class="value"><code>—</code></div></div>
        <div class="field"><div class="label">تلاش این الگو</div><div id="current-attempts" class="value">—</div></div>
        <div class="field"><div class="label">زمان شروع الگو</div><div id="current-started" class="value">—</div></div>
        <div class="field"><div class="label">آخرین خطای این الگو</div><div id="current-error" class="value error">—</div></div>
      </div>
    </section>

    <section class="panel">
      <h2>وضعیت محصولات</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>محصول</th><th>وضعیت</th><th>شماره یکتا</th><th>پیش‌شماره‌ها</th><th>الگوها</th><th>الگوی فعلی/بعدی</th><th>فایل‌ها</th></tr></thead>
          <tbody id="products-body"><tr><td colspan="7" class="muted">در حال خواندن…</td></tr></tbody>
        </table>
      </div>
    </section>

    <section class="panel">
      <h2>آخرین رویدادها</h2>
      <p id="last-event" class="notice" style="margin-bottom:10px">—</p>
      <pre id="log" class="log">در حال خواندن لاگ…</pre>
    </section>

    <p class="notice">این داشبورد هیچ درخواست جدیدی به ایرانسل نمی‌فرستد و فقط فایل‌های محلی را می‌خواند. برای بستن داشبورد، پنجرهٔ اجرای آن را ببندید.</p>
  </div>
  <script>
    const nf = new Intl.NumberFormat('fa-IR');
    const statusLabels = {running:'در حال استخراج', complete:'کامل', queued:'در صف', waiting:'در انتظار', paused:'متوقف با state ذخیره‌شده', skipped:'رد شده', error:'خطا'};
    const statusClasses = {running:'s-running', complete:'s-complete', queued:'s-queued', waiting:'s-waiting', paused:'s-paused', skipped:'s-skipped', error:'s-error'};
    const $ = (id) => document.getElementById(id);
    function fmt(n) { return nf.format(Number(n || 0)); }
    function esc(v) { return String(v ?? '—').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c])); }
    function pattern(v) { return v ? '<code>' + esc(v) + '</code>' : '<span class="muted">—</span>'; }
    function statusBadge(p) { const c = statusClasses[p.status] || 's-paused'; return '<span class="status ' + c + '">' + esc(statusLabels[p.status] || p.status) + '</span>'; }
    function setText(id, value) { $(id).textContent = value; }
    function render(s) {
      const bot = s.bot || {};
      const badge = $('bot-badge');
      badge.textContent = bot.running ? 'ربات فعال است · PID ' + (bot.pids || []).join(', ') : 'ربات اجرا نمی‌شود';
      badge.className = 'badge ' + (bot.running ? 'running' : 'offline');
      setText('total-numbers', fmt(s.totals.numbers));
      setText('total-pending', fmt(s.totals.pending_patterns));
      setText('complete-products', fmt(s.totals.complete_products) + ' از ' + fmt(s.totals.products));
      setText('updated-at', s.generated_at || '—');

      const current = s.current;
      $('current-empty').hidden = Boolean(current);
      $('current-fields').hidden = !current;
      if (current) {
        $('current-heading').textContent = current.phase === 'next' ? 'الگوی بعدی در صف' : 'استخراج فعلی';
        setText('current-product', (current.product_id || '—') + ' · ' + (current.product_name || ''));
        $('current-prefix').innerHTML = pattern(current.prefix);
        $('current-pattern').innerHTML = pattern(current.pattern);
        setText('current-attempts', fmt(current.attempts));
        setText('current-started', current.started_at || '—');
        setText('current-error', current.last_error || 'بدون خطا');
      }

      $('products-body').innerHTML = (s.products || []).map(p => {
        const files = Object.entries(p.files || {}).filter(([, exists]) => exists).map(([name]) => name).join('، ') || '—';
        const patterns = 'کامل: ' + fmt(p.pattern_counts.done) + '<br>در صف: ' + fmt((p.pattern_counts.pending || 0) + (p.pattern_counts.in_progress || 0));
        const currentPattern = p.current_pattern || p.next_pattern || '';
        return '<tr>' +
          '<td><b>' + esc(p.product_id) + '</b><br><span class="muted">' + esc(p.product_name) + '</span></td>' +
          '<td>' + statusBadge(p) + '</td>' +
          '<td class="mono">' + fmt(p.number_count) + '</td>' +
          '<td class="mono">' + esc((p.prefixes || []).join(', ') || '—') + '</td>' +
          '<td>' + patterns + '</td>' +
          '<td>' + pattern(currentPattern) + '</td>' +
          '<td class="muted">' + esc(files) + '</td>' +
          '</tr>';
      }).join('') || '<tr><td colspan="7" class="muted">stateای پیدا نشد.</td></tr>';

      setText('last-event', s.log.last_line || 'هنوز رویدادی ثبت نشده است.');
      $('log').textContent = (s.log.recent || []).join('\n') || 'لاگی ثبت نشده است.';
    }
    async function refresh() {
      try {
        const response = await fetch('/api/status?_=' + Date.now(), {cache:'no-store'});
        if (!response.ok) throw new Error('HTTP ' + response.status);
        render(await response.json());
      } catch (error) {
        $('bot-badge').textContent = 'خطا در اتصال به داشبورد';
        $('bot-badge').className = 'badge offline';
        setText('last-event', error.message);
      }
    }
    refresh();
    setInterval(refresh, 3000);
  </script>
</body>
</html>
"""


def load_json(path: pathlib.Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def product_configs() -> dict[int, dict[str, Any]]:
    try:
        data = json.loads((ROOT / "products.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = []
    result: dict[int, dict[str, Any]] = {}
    for item in data if isinstance(data, list) else []:
        try:
            result[int(item["id"])] = item
        except (KeyError, TypeError, ValueError):
            continue
    return result


def running_bot_pids() -> list[str]:
    command = (
        "Get-CimInstance -ClassName Win32_Process -Filter \"Name = 'python.exe'\" "
        "| Where-Object { $_.CommandLine -match 'irancell_number_bot\\.py' } "
        "| Select-Object -ExpandProperty ProcessId"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip().isdigit()]


def tail_lines(path: pathlib.Path, limit: int = 12) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-limit:]
    except OSError:
        return []


def read_meta(con: sqlite3.Connection) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        rows = con.execute("SELECT key, value FROM meta").fetchall()
    except sqlite3.Error:
        return result
    for row in rows:
        try:
            result[str(row[0])] = json.loads(row[1])
        except (TypeError, json.JSONDecodeError):
            result[str(row[0])] = row[1]
    return result


def read_product(db_path: pathlib.Path, configs: dict[int, dict[str, Any]], bot_running: bool) -> dict[str, Any]:
    directory = db_path.parent
    folder_id = directory.name.split("_", 1)[0]
    try:
        fallback_id = int(folder_id)
    except ValueError:
        fallback_id = 0
    product_config = configs.get(fallback_id, {})
    metadata = load_json(directory / "metadata.json")
    summary = load_json(directory / "summary.json")
    result: dict[str, Any] = {
        "product_id": fallback_id,
        "product_name": product_config.get("name") or metadata.get("info", {}).get("fa", {}).get("name") or directory.name,
        "folder": directory.name,
        "prefixes": metadata.get("prefixes") or [],
        "number_count": 0,
        "pattern_counts": {"pending": 0, "in_progress": 0, "done": 0},
        "attempts": 0,
        "current_pattern": None,
        "next_pattern": None,
        "last_error": None,
        "latest_discovered": None,
        "files": {name: (directory / name).exists() for name in ("numbers.txt", "numbers.csv", "numbers.partial.txt", "summary.json", "metadata.json")},
        "status": "paused",
        "read_error": None,
    }
    try:
        with sqlite3.connect(db_path, timeout=2) as con:
            con.row_factory = sqlite3.Row
            meta = read_meta(con)
            product_meta = meta.get("product") or {}
            result["product_id"] = int(product_meta.get("id", fallback_id))
            result["product_name"] = product_meta.get("name") or result["product_name"]
            result["prefixes"] = meta.get("prefixes") or result["prefixes"]
            result["number_count"] = int(con.execute("SELECT COUNT(*) FROM numbers").fetchone()[0])
            counts = con.execute("SELECT status, COUNT(*) AS count FROM patterns GROUP BY status").fetchall()
            result["pattern_counts"] = {str(row["status"]): int(row["count"]) for row in counts}
            result["attempts"] = int(con.execute("SELECT COALESCE(SUM(attempts), 0) FROM patterns").fetchone()[0])
            result["latest_discovered"] = con.execute("SELECT MAX(discovered_at) FROM numbers").fetchone()[0]
            active = con.execute(
                "SELECT prefix, pattern, attempts, started_at, last_error "
                "FROM patterns WHERE status='in_progress' ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            next_row = con.execute(
                "SELECT prefix, pattern FROM patterns WHERE status='pending' ORDER BY id LIMIT 1"
            ).fetchone()
            last_error = con.execute(
                "SELECT last_error FROM patterns WHERE last_error IS NOT NULL AND last_error <> '' "
                "ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if active:
                result["current_pattern"] = active["pattern"]
                result["current_prefix"] = active["prefix"]
                result["current_attempts"] = int(active["attempts"])
                result["current_started"] = active["started_at"]
                result["current_error"] = active["last_error"]
            if next_row:
                result["next_pattern"] = next_row["pattern"]
                result["next_prefix"] = next_row["prefix"]
            result["last_error"] = (last_error[0] if last_error else None) or summary.get("last_error")
    except (OSError, sqlite3.Error, ValueError) as exc:
        result["read_error"] = str(exc)

    counts = result["pattern_counts"]
    pending = int(counts.get("pending", 0))
    in_progress = int(counts.get("in_progress", 0))
    summary_status = str(summary.get("status") or "")
    if result["read_error"]:
        result["status"] = "error"
    elif in_progress and bot_running:
        result["status"] = "running"
    elif pending:
        result["status"] = "queued" if bot_running else "paused"
    elif summary_status == "skipped_no_number_selection":
        result["status"] = "skipped"
    else:
        result["status"] = "complete"
    return result


def read_status(output_root: pathlib.Path) -> dict[str, Any]:
    output_root = output_root.resolve()
    bot_pids = running_bot_pids()
    bot_running = bool(bot_pids)
    configs = product_configs()
    products = [read_product(path, configs, bot_running) for path in sorted(output_root.glob("*/state.sqlite3"))]
    products.sort(key=lambda item: int(item.get("product_id") or 0))
    total_numbers = sum(int(item["number_count"]) for item in products)
    total_pending = sum(
        int(item["pattern_counts"].get("pending", 0)) + int(item["pattern_counts"].get("in_progress", 0))
        for item in products
    )
    complete_products = sum(1 for item in products if item["status"] == "complete")

    current_product = next((item for item in products if item.get("current_pattern")), None)
    if current_product and bot_running:
        current = {
            "product_id": current_product["product_id"],
            "product_name": current_product["product_name"],
            "prefix": current_product.get("current_prefix"),
            "pattern": current_product.get("current_pattern"),
            "attempts": current_product.get("current_attempts", 0),
            "started_at": current_product.get("current_started"),
            "last_error": current_product.get("current_error"),
            "phase": "active",
        }
    else:
        queued = next((item for item in products if item.get("next_pattern")), None)
        current = None
        if queued and bot_running:
            current = {
                "product_id": queued["product_id"],
                "product_name": queued["product_name"],
                "prefix": queued.get("next_prefix"),
                "pattern": queued.get("next_pattern"),
                "attempts": 0,
                "started_at": None,
                "last_error": queued.get("last_error"),
                "phase": "next",
            }

    log_path = output_root / "run.log"
    recent = tail_lines(log_path)
    last_line = recent[-1] if recent else None
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "bot": {"running": bot_running, "pids": bot_pids},
        "totals": {
            "numbers": total_numbers,
            "pending_patterns": total_pending,
            "products": len(products),
            "complete_products": complete_products,
        },
        "current": current,
        "products": products,
        "log": {"last_line": last_line, "recent": recent},
    }


class DashboardHandler(BaseHTTPRequestHandler):
    output_root = DEFAULT_OUTPUT

    def log_message(self, _format: str, *_args: Any) -> None:
        return

    def send_bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # The browser may close an auto-refresh request; it is not a dashboard error.
            pass

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        path = urlsplit(self.path).path
        if path in {"/", "/index.html"}:
            self.send_bytes(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/status":
            payload = json.dumps(read_status(self.output_root), ensure_ascii=False).encode("utf-8")
            self.send_bytes(payload, "application/json; charset=utf-8")
            return
        if path == "/health":
            self.send_bytes(b'{"ok":true}', "application/json; charset=utf-8")
            return
        self.send_bytes(b"Not Found", "text/plain; charset=utf-8", 404)


class DashboardServer(ThreadingHTTPServer):
    allow_reuse_address = True


def main() -> int:
    parser = argparse.ArgumentParser(description="داشبورد محلی وضعیت استخراج ایرانسل")
    parser.add_argument("--output", type=pathlib.Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--host", default="127.0.0.1", help="فقط 127.0.0.1 برای عدم انتشار روی شبکه")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    output_root = args.output.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    DashboardHandler.output_root = output_root
    url = f"http://{args.host}:{args.port}/"
    try:
        server = DashboardServer((args.host, args.port), DashboardHandler)
    except OSError as exc:
        print(f"پورت {args.port} قابل استفاده نیست: {exc}", file=sys.stderr)
        return 2

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"داشبورد آماده است: {url}")
    print("برای توقف، همین پنجره را با Ctrl+C ببندید.")
    if args.open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
