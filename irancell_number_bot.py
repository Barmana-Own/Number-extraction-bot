#!/usr/bin/env python3
"""Resume-friendly extractor for the public number-selection data in Irancell Shop.

The shop UI exposes a public JSON endpoint for the number selector.  The UI only
renders a small window and its offset pagination can repeat the same 100-item
block, so this bot enumerates the wildcard pattern tree instead of trusting the
UI's next-page button.

The program only reads public product metadata and number-search results.  It
does not log in, request OTPs, add anything to a cart, or submit a purchase.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


ROOT = Path(__file__).resolve().parent
PRODUCTS_FILE = ROOT / "products.json"
DEFAULT_OUTPUT = ROOT / "output"
API_BASE = os.environ.get("IRANCELL_API_BASE_URL", "https://apishop.irancell.ir").rstrip("/")
SHOP_ORIGIN = os.environ.get("IRANCELL_SHOP_ORIGIN", "https://shop.irancell.ir").rstrip("/")
SEARCH_PATH = "/shop/api/v2/search_msisdns"
PRODUCT_PATH = "/shop/api/v2/get_product_by_id"
NUMBER_DIGITS = 10
DEFAULT_QUERY_CAP = 100
PERSIAN_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
ExtractionEvent = Callable[[str, dict[str, Any]], None]


def emit_event(callback: ExtractionEvent | None, event: str, **payload: Any) -> None:
    if callback is None:
        return
    try:
        callback(event, payload)
    except Exception:
        # Observability must never corrupt the resumable extraction state.
        logging.debug("extraction event callback failed", exc_info=True)


def normalize_bot_prefix(value: str) -> str:
    digits = normalize_digits(str(value))
    if len(digits) == 4 and digits.startswith("0"):
        return digits[1:]
    return digits


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def atomic_write_text(path: Path, text: str) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def normalize_digits(value: str) -> str:
    """Return a comparable 10-digit representation of a displayed number."""

    digits = re.sub(r"\D", "", value.translate(PERSIAN_DIGITS))
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    elif len(digits) == 12 and digits.startswith("98"):
        digits = digits[2:]
    return digits


def parse_format_pattern(value: str | None, prefix: str) -> list[int]:
    if value:
        try:
            parts = [int(part) for part in value.split("-")]
            if sum(parts) == NUMBER_DIGITS and all(part > 0 for part in parts):
                return parts
        except ValueError:
            pass
    # The 0900 products are displayed as 900 - 1234 - 567; ordinary Iranian
    # mobile products in this shop are displayed as 935 - 123 - 4567.
    return [3, 4, 3] if prefix == "900" else [3, 3, 4]


def format_number(digits: str, format_pattern: str | None, prefix: str) -> str:
    parts = parse_format_pattern(format_pattern, prefix)
    result: list[str] = []
    cursor = 0
    for length in parts:
        result.append(digits[cursor : cursor + length])
        cursor += length
    if cursor != len(digits):
        return digits
    return " - ".join(result)


def children_for_pattern(pattern: str) -> list[str]:
    """Replace the first wildcard with each decimal digit."""

    index = pattern.find("*")
    if index < 0:
        return []
    return [pattern[:index] + str(digit) + pattern[index + 1 :] for digit in range(10)]


def result_code(payload: dict[str, Any]) -> int | None:
    value = payload.get("result_code")
    if value is None and isinstance(payload.get("result"), dict):
        value = payload["result"].get("result_code")
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


class ApiFailure(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ApiClient:
    def __init__(
        self,
        *,
        delay: float,
        max_retries: int,
        retry_forever_on_429: bool = False,
        rate_limit_cooldown: float = 600.0,
        timeout: float = 45.0,
        event_callback: ExtractionEvent | None = None,
    ) -> None:
        self.delay = max(0.0, delay)
        self.max_retries = max(0, max_retries)
        self.retry_forever_on_429 = retry_forever_on_429
        self.rate_limit_cooldown = max(1.0, rate_limit_cooldown)
        self.timeout = timeout
        self.event_callback = event_callback
        self.last_request_at = 0.0
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))

    def _wait_for_rate_limit(self) -> None:
        elapsed = time.monotonic() - self.last_request_at
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self.last_request_at = time.monotonic()

    def _retry_after(self, headers: dict[str, str], attempt: int, *, rate_limited: bool) -> float:
        raw = headers.get("Retry-After", "")
        try:
            advertised = max(1.0, float(raw))
            if rate_limited:
                return max(self.rate_limit_cooldown, advertised)
            return min(120.0, advertised)
        except ValueError:
            # A little jitter prevents several resumed processes from lining up.
            backoff = max(2.0, 2.0 ** min(attempt, 6)) + random.uniform(0.0, 0.5)
            return max(self.rate_limit_cooldown, backoff) if rate_limited else min(120.0, backoff)

    def post_json(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        referer: str,
    ) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": SHOP_ORIGIN,
            "Referer": referer,
            "User-Agent": "irancell-number-bot/1.0 (respectful public-data reader)",
            "channel": "eShop",
        }

        attempt = 0
        while True:
            self._wait_for_rate_limit()
            request = Request(API_BASE + path, data=body, headers=headers, method="POST")
            status = 0
            response_headers: dict[str, str] = {}
            raw = b""
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    status = int(response.status)
                    response_headers = {key: value for key, value in response.headers.items()}
                    raw = response.read()
            except HTTPError as exc:
                status = int(exc.code)
                response_headers = {key: value for key, value in exc.headers.items()}
                raw = exc.read()
            except (URLError, TimeoutError, OSError) as exc:
                if attempt < self.max_retries:
                    wait = self._retry_after({}, attempt, rate_limited=False)
                    emit_event(self.event_callback, "on_error", message=str(exc), retryable=True, retry_in_seconds=wait)
                    logging.warning("ارتباط با API قطع شد؛ تلاش مجدد در %.1f ثانیه: %s", wait, exc)
                    time.sleep(wait)
                    attempt += 1
                    continue
                raise ApiFailure(f"network error after retries: {exc}", retryable=True) from exc

            try:
                decoded = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                if attempt < self.max_retries and status >= 500:
                    time.sleep(self._retry_after(response_headers, attempt, rate_limited=False))
                    attempt += 1
                    continue
                raise ApiFailure(f"invalid API response (HTTP {status})", retryable=status >= 500) from exc

            code = result_code(decoded) if isinstance(decoded, dict) else None
            rate_limited = status == 429 or code == 429
            retryable = rate_limited or status >= 500 or code in {500, 502, 503, 504}
            if retryable:
                can_retry = attempt < self.max_retries or (self.retry_forever_on_429 and rate_limited)
                if can_retry:
                    wait = self._retry_after(response_headers, attempt, rate_limited=rate_limited)
                    if rate_limited:
                        emit_event(self.event_callback, "on_rate_limited", status=status, code=code, retry_in_seconds=wait, attempt=attempt + 1)
                    else:
                        emit_event(self.event_callback, "on_error", status=status, code=code, retryable=True, retry_in_seconds=wait, attempt=attempt + 1)
                    logging.warning(
                        "API محدود/موقتاً ناموفق بود (HTTP %s، کد %s)؛ تلاش مجدد در %.1f ثانیه%s",
                        status,
                        code,
                        wait,
                        " (تلاش نامحدود برای 429)" if self.retry_forever_on_429 and rate_limited else "",
                    )
                    time.sleep(wait)
                    attempt += 1
                    continue
                raise ApiFailure(
                    f"API remained unavailable after retries (HTTP {status}, code {code})",
                    retryable=True,
                )

            if status >= 400:
                raise ApiFailure(f"API rejected request (HTTP {status}, code {code})")
            if code not in (None, 0):
                raise ApiFailure(f"API returned result_code={code}")
            if not isinstance(decoded, dict):
                raise ApiFailure("API response was not a JSON object")
            return decoded

    def get_product(self, product_id: int, referer: str) -> dict[str, Any]:
        return self.post_json(PRODUCT_PATH, {"channel": "eShop", "id": product_id}, referer=referer)

    def search_numbers(self, product_id: int, pattern: str, referer: str) -> dict[str, Any]:
        return self.post_json(
            SEARCH_PATH,
            {"channel": "eShop", "productId": product_id, "pattern": pattern, "offset": 0},
            referer=referer,
        )


class ProductStore:
    def __init__(self, directory: Path, *, expected_length: int = NUMBER_DIGITS) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.expected_length = expected_length
        self.db = sqlite3.connect(self.directory / "state.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prefix TEXT NOT NULL,
                pattern TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                result_count INTEGER,
                last_error TEXT,
                started_at TEXT,
                finished_at TEXT,
                UNIQUE(prefix, pattern)
            );
            CREATE TABLE IF NOT EXISTS numbers (
                number TEXT PRIMARY KEY,
                formatted_number TEXT NOT NULL,
                prefix TEXT NOT NULL,
                first_pattern TEXT NOT NULL,
                raw_number TEXT NOT NULL,
                discovered_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_patterns_status ON patterns(status, id);
            """
        )
        self.db.execute("UPDATE patterns SET status='pending' WHERE status='in_progress'")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def set_meta(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )
        self.db.commit()

    def ensure_roots(self, prefixes: Iterable[str], suffix_length: int, start_tail: str | None = None) -> None:
        for prefix in prefixes:
            tail = re.sub(r"\D", "", str(start_tail or ""))
            if tail and len(tail) <= suffix_length:
                pattern = prefix + tail + ("*" * (suffix_length - len(tail)))
            else:
                pattern = prefix + ("*" * suffix_length)
            self.db.execute(
                "INSERT OR IGNORE INTO patterns(prefix, pattern) VALUES(?, ?)",
                (prefix, pattern),
            )
        self.db.commit()

    def claim_next(self, prefix: str | None = None, pattern_startswith: str | None = None) -> sqlite3.Row | None:
        conditions = ["status='pending'"]
        params: list[Any] = []
        if prefix:
            conditions.append("prefix=?")
            params.append(prefix)
        if pattern_startswith:
            conditions.append("pattern LIKE ?")
            params.append(pattern_startswith + "%")
        row = self.db.execute(
            "SELECT id, prefix, pattern, attempts FROM patterns "
            f"WHERE {' AND '.join(conditions)} ORDER BY id LIMIT 1",
            tuple(params),
        ).fetchone()
        if row is None:
            return None
        self.db.execute(
            "UPDATE patterns SET status='in_progress', attempts=attempts+1, "
            "started_at=?, last_error=NULL WHERE id=?",
            (now_utc(), row["id"]),
        )
        self.db.commit()
        return row

    def add_children(self, prefix: str, pattern: str) -> int:
        children = children_for_pattern(pattern)
        for child in children:
            self.db.execute(
                "INSERT OR IGNORE INTO patterns(prefix, pattern) VALUES(?, ?)",
                (prefix, child),
            )
        self.db.commit()
        return len(children)

    def insert_numbers(
        self,
        values: Iterable[str],
        *,
        prefix: str,
        pattern: str,
        format_pattern: str | None,
    ) -> list[tuple[str, str]]:
        new_values: list[tuple[str, str]] = []
        for raw in values:
            raw_text = str(raw)
            number = normalize_digits(raw_text)
            if len(number) != self.expected_length or not number.startswith(prefix):
                logging.warning("ردیف غیرقابل‌تشخیص نادیده گرفته شد: %r", raw_text)
                continue
            formatted = format_number(number, format_pattern, prefix)
            before = self.db.total_changes
            self.db.execute(
                "INSERT OR IGNORE INTO numbers "
                "(number, formatted_number, prefix, first_pattern, raw_number, discovered_at) "
                "VALUES(?, ?, ?, ?, ?, ?)",
                (number, formatted, prefix, pattern, raw_text, now_utc()),
            )
            if self.db.total_changes > before:
                new_values.append((number, formatted))
        self.db.commit()
        return new_values

    def mark_done(self, pattern_id: int, result_count: int) -> None:
        self.db.execute(
            "UPDATE patterns SET status='done', result_count=?, finished_at=?, last_error=NULL WHERE id=?",
            (result_count, now_utc(), pattern_id),
        )
        self.db.commit()

    def requeue(self, pattern_id: int, error: str) -> None:
        self.db.execute(
            "UPDATE patterns SET status='pending', last_error=? WHERE id=?",
            (error[:1000], pattern_id),
        )
        self.db.commit()

    def pending_count(self, prefix: str | None = None) -> int:
        if prefix:
            row = self.db.execute(
                "SELECT COUNT(*) AS count FROM patterns WHERE status IN ('pending', 'in_progress') AND prefix=?",
                (prefix,),
            ).fetchone()
        else:
            row = self.db.execute(
            "SELECT COUNT(*) AS count FROM patterns WHERE status IN ('pending', 'in_progress')"
            ).fetchone()
        return int(row["count"])

    def scanned_count(self, prefix: str | None = None) -> int:
        if prefix:
            row = self.db.execute(
                "SELECT COUNT(*) AS count FROM patterns WHERE status='done' AND prefix=?",
                (prefix,),
            ).fetchone()
        else:
            row = self.db.execute("SELECT COUNT(*) AS count FROM patterns WHERE status='done'").fetchone()
        return int(row["count"])

    def number_count(self, prefix: str | None = None) -> int:
        if prefix:
            row = self.db.execute("SELECT COUNT(*) AS count FROM numbers WHERE prefix=?", (prefix,)).fetchone()
        else:
            row = self.db.execute("SELECT COUNT(*) AS count FROM numbers").fetchone()
        return int(row["count"])

    def pattern_counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) AS count FROM patterns GROUP BY status").fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def append_partial(self, values: Iterable[tuple[str, str]]) -> None:
        values = list(values)
        if not values:
            return
        path = self.directory / "numbers.partial.txt"
        with path.open("a", encoding="utf-8") as handle:
            for number, _formatted in values:
                handle.write(number + "\n")

    def export(self, *, status: str, product: dict[str, Any], metadata: dict[str, Any] | None) -> None:
        rows = self.db.execute(
            "SELECT number, formatted_number, prefix, first_pattern, raw_number "
            "FROM numbers ORDER BY number"
        ).fetchall()
        txt_path = self.directory / "numbers.txt"
        csv_path = self.directory / "numbers.csv"
        atomic_write_text(txt_path, "".join(f"{row['number']}\n" for row in rows))
        temp_csv = csv_path.with_name(csv_path.name + ".tmp")
        with temp_csv.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["number", "formatted_number", "prefix", "pattern", "raw_number"])
            for row in rows:
                writer.writerow(
                    [
                        row["number"],
                        row["formatted_number"],
                        row["prefix"],
                        row["first_pattern"],
                        row["raw_number"],
                    ]
                )
        temp_csv.replace(csv_path)
        summary = {
            "updated_at": now_utc(),
            "status": status,
            "product_id": product["id"],
            "product_name": product.get("name", ""),
            "number_count": len(rows),
            "pending_patterns": self.pending_count(),
            "pattern_counts": self.pattern_counts(),
            "metadata_prefixes": (metadata or {}).get("prefixes", []),
        }
        atomic_write_json(self.directory / "summary.json", summary)


@dataclass(frozen=True)
class ProductConfig:
    id: int
    slug: str
    name: str


def load_products() -> list[ProductConfig]:
    raw = json.loads(PRODUCTS_FILE.read_text(encoding="utf-8"))
    return [ProductConfig(int(item["id"]), item["slug"], item["name"]) for item in raw]


def select_products(products: list[ProductConfig], selection: str) -> list[ProductConfig]:
    if selection.strip().lower() in {"all", "همه"}:
        return products
    wanted = {int(part.strip()) for part in selection.split(",") if part.strip()}
    selected = [product for product in products if product.id in wanted]
    missing = wanted - {product.id for product in selected}
    if missing:
        raise SystemExit(f"شناسه محصول نامعتبر است: {', '.join(map(str, sorted(missing)))}")
    return selected


def product_directory(output_root: Path, product: ProductConfig) -> Path:
    return output_root / f"{product.id}_{product.slug}"


def local_number_count(output_root: Path, product: ProductConfig, prefix: str | None = None) -> int:
    """Read the resumable local count without starting an extraction request."""
    directory = product_directory(output_root, product)
    if not (directory / "state.sqlite3").exists():
        return 0
    store = ProductStore(directory)
    try:
        return store.number_count(normalize_bot_prefix(prefix) if prefix else None)
    finally:
        store.close()


def extract_product(
    client: ApiClient,
    product: ProductConfig,
    *,
    output_root: Path,
    max_requests: int,
    prefix_filter: str | None = None,
    start_tail: str = "",
    max_numbers: int = 0,
    should_stop: Callable[[], str | bool] | None = None,
    on_event: ExtractionEvent | None = None,
    baseline_number_count: int | None = None,
    request_offset: int = 0,
) -> dict[str, Any]:
    directory = product_directory(output_root, product)
    store = ProductStore(directory)
    referer = f"{SHOP_ORIGIN}/fa/product/{product.id}/"
    request_count = 0
    metadata: dict[str, Any] | None = None
    status = "running"
    result: dict[str, Any] | None = None
    requested_prefix = normalize_bot_prefix(prefix_filter) if prefix_filter else None
    requested_tail = re.sub(r"\D", "", str(start_tail or ""))
    initial_number_count = 0
    safe_request_offset = max(0, int(request_offset))
    emit_event(on_event, "on_started", product_id=product.id, product_name=product.name)
    try:
        metadata = client.get_product(product.id, referer)
        atomic_write_json(directory / "metadata.json", metadata)
        store.set_meta("product", product.__dict__)
        store.set_meta("metadata_fetched_at", now_utc())

        addons = metadata.get("addons") or []
        prefixes = [normalize_bot_prefix(str(prefix)) for prefix in (metadata.get("prefixes") or [])]
        prefixes = [prefix for prefix in prefixes if re.fullmatch(r"\d{3}", prefix)]
        if requested_prefix:
            prefixes = [prefix for prefix in prefixes if normalize_bot_prefix(prefix) == requested_prefix]
            if not prefixes:
                raise ApiFailure(f"requested prefix is not available for product {product.id}")
        store.set_meta("prefixes", prefixes)
        emit_event(on_event, "on_product_started", product_id=product.id, prefixes=prefixes)

        if "numberSelection" not in addons or not prefixes:
            status = "skipped_no_number_selection"
            logging.info("[%s] بخش انتخاب شماره ندارد؛ رد شد", product.id)
            return {"product_id": product.id, "status": status, "number_count": 0}

        suffix_length = NUMBER_DIGITS - len(prefixes[0])
        if suffix_length <= 0 or any(len(prefix) + suffix_length != NUMBER_DIGITS for prefix in prefixes):
            raise ApiFailure(f"prefix length is not compatible with {NUMBER_DIGITS}-digit numbers")
        if len(requested_tail) > suffix_length:
            raise ApiFailure("requested tail is longer than the available number suffix")
        store.ensure_roots(prefixes, suffix_length, requested_tail or None)
        initial_number_count = max(0, int(baseline_number_count)) if baseline_number_count is not None else store.number_count(requested_prefix)

        format_pattern = metadata.get("format_pattern")
        logging.info(
            "[%s] شروع استخراج؛ پیش‌شماره‌ها: %s",
            product.id,
            ", ".join(prefixes),
        )
        while True:
            decision = should_stop() if should_stop else False
            if decision in (True, "stop"):
                status = "stopped"
                break
            if decision == "pause":
                status = "paused_by_user"
                break
            if max_requests and request_count >= max_requests:
                status = "paused_request_limit"
                logging.info("[%s] سقف درخواست این اجرا رسید؛ دفعه بعد ادامه می‌دهد", product.id)
                break
            if max_numbers and store.number_count(requested_prefix) >= max_numbers:
                status = "target_reached"
                break
            pattern_scope = (prefixes[0] + requested_tail) if requested_prefix and requested_tail else None
            row = store.claim_next(prefix=prefixes[0] if requested_prefix else None, pattern_startswith=pattern_scope)
            if row is None:
                status = "complete"
                break
            try:
                response = client.search_numbers(product.id, row["pattern"], referer)
                request_count += 1
                values = response.get("numbers") or []
                new_values = store.insert_numbers(
                    values,
                    prefix=row["prefix"],
                    pattern=row["pattern"],
                    format_pattern=format_pattern,
                )
                store.append_partial(new_values)
                for number, formatted in new_values:
                    emit_event(
                        on_event,
                        "on_number_found",
                        product_id=product.id,
                        prefix=row["prefix"],
                        pattern=row["pattern"],
                        number=number,
                        formatted_number=formatted,
                    )

                cap = response.get("limit")
                try:
                    cap = int(cap) if cap else DEFAULT_QUERY_CAP
                except (TypeError, ValueError):
                    cap = DEFAULT_QUERY_CAP
                reached_target = bool(max_numbers and store.number_count(requested_prefix) >= max_numbers)
                if reached_target:
                    status = "target_reached"
                elif len(values) >= cap and "*" in row["pattern"]:
                    child_count = store.add_children(row["prefix"], row["pattern"])
                    logging.debug(
                        "[%s] الگوی %s پر بود؛ %s زیرالگو اضافه شد",
                        product.id,
                        row["pattern"],
                        child_count,
                    )
                elif len(values) >= cap and "*" not in row["pattern"]:
                    logging.warning("[%s] الگوی کامل با پاسخ سقف‌خورده برگشت: %s", product.id, row["pattern"])
                store.mark_done(row["id"], len(values))
                emit_event(
                    on_event,
                    "on_progress",
                    product_id=product.id,
                    requests=safe_request_offset + request_count,
                    scanned_patterns=store.scanned_count(requested_prefix),
                    pending_patterns=store.pending_count(requested_prefix),
                    unique_numbers=max(0, store.number_count(requested_prefix) - initial_number_count),
                    current_pattern=row["pattern"],
                )
                if reached_target:
                    break
                if request_count == 1 or request_count % 25 == 0:
                    logging.info(
                        "[%s] درخواست %s؛ شماره یکتا: %s؛ الگوهای باقی‌مانده: %s",
                        product.id,
                        request_count,
                        store.number_count(requested_prefix),
                        store.pending_count(requested_prefix),
                    )
            except Exception as exc:
                store.requeue(row["id"], str(exc))
                emit_event(on_event, "on_error", product_id=product.id, pattern=row["pattern"], message=str(exc), retryable=False)
                raise

        result = {
            "product_id": product.id,
            "status": status,
            "number_count": store.number_count(requested_prefix),
            "new_number_count": max(0, store.number_count(requested_prefix) - initial_number_count),
            "requests": safe_request_offset + request_count,
            "scanned_patterns": store.scanned_count(requested_prefix),
            "pending_patterns": store.pending_count(requested_prefix),
        }
        return result
    except KeyboardInterrupt:
        status = "interrupted"
        raise
    except Exception as exc:
        status = "error"
        emit_event(on_event, "on_error", product_id=product.id, message=str(exc), retryable=isinstance(exc, ApiFailure) and exc.retryable)
        raise
    finally:
        if metadata is None:
            status = "metadata_error"
        try:
            store.export(status=status, product=product.__dict__, metadata=metadata)
        finally:
            store.close()
        emit_event(
            on_event,
            "on_completed",
            product_id=product.id,
            status=status,
            number_count=(result or {}).get("number_count", 0),
            new_number_count=(result or {}).get("new_number_count", 0),
            requests=(result or {}).get("requests", safe_request_offset + request_count),
            pending_patterns=(result or {}).get("pending_patterns", 0),
        )


def export_existing(products: list[ProductConfig], output_root: Path) -> None:
    for product in products:
        directory = product_directory(output_root, product)
        db_path = directory / "state.sqlite3"
        if not db_path.exists():
            logging.info("[%s] state.sqlite3 پیدا نشد؛ رد شد", product.id)
            continue
        store = ProductStore(directory)
        try:
            metadata_path = directory / "metadata.json"
            metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else None
            store.export(status="exported", product=product.__dict__, metadata=metadata)
            logging.info("[%s] خروجی‌ها بازسازی شد؛ شماره یکتا: %s", product.id, store.number_count())
        finally:
            store.close()


def configure_logging(output_root: Path, verbose: bool) -> None:
    output_root.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()
    configure_console_encoding()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root_logger.addHandler(console)
    file_handler = logging.FileHandler(output_root / "run.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)


def configure_console_encoding() -> None:
    """Keep CLI help and logs usable on legacy Windows code pages."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="استخراج قابل‌ادامه شماره‌های عمومی فروشگاه ایرانسل")
    parser.add_argument("--products", default="all", help="all یا فهرست شناسه‌ها با کاما؛ مثل 565,560")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="پوشه خروجی")
    parser.add_argument("--delay", type=float, default=1.0, help="حداقل فاصله بین درخواست‌ها، برحسب ثانیه")
    parser.add_argument("--max-retries", type=int, default=6, help="تعداد تلاش مجدد برای خطا/429")
    parser.add_argument(
        "--retry-forever-429",
        action="store_true",
        help="برای کد 429 تا رفع محدودیت به‌صورت نامحدود، با مکث، تلاش کن",
    )
    parser.add_argument(
        "--rate-limit-cooldown",
        type=float,
        default=600.0,
        help="حداقل مکث روی 429، برحسب ثانیه",
    )
    parser.add_argument("--max-requests", type=int, default=0, help="سقف درخواست در این اجرا؛ صفر یعنی بدون سقف")
    parser.add_argument("--export-only", action="store_true", help="فقط CSV/TXT را از state.sqlite3 بازسازی کن")
    parser.add_argument("--verbose", action="store_true", help="نمایش جزئیات الگوها")
    return parser


def main() -> int:
    configure_console_encoding()
    args = build_parser().parse_args()
    products = load_products()
    selected = select_products(products, args.products)
    configure_logging(args.output, args.verbose)

    if args.export_only:
        export_existing(selected, args.output)
        return 0

    client = ApiClient(
        delay=args.delay,
        max_retries=args.max_retries,
        retry_forever_on_429=args.retry_forever_429,
        rate_limit_cooldown=args.rate_limit_cooldown,
    )
    failures: list[int] = []
    for product in selected:
        try:
            result = extract_product(
                client,
                product,
                output_root=args.output,
                max_requests=max(0, args.max_requests),
            )
            logging.info("[%s] پایان: %s", product.id, json.dumps(result, ensure_ascii=False))
        except KeyboardInterrupt:
            logging.warning("توقف دستی؛ state ذخیره شده و اجرای بعدی ادامه می‌دهد")
            return 130
        except Exception as exc:
            failures.append(product.id)
            logging.error("[%s] این محصول متوقف شد و برای اجرای بعد pending ماند: %s", product.id, exc)
            # Stop after an API failure so a rate-limit or outage is not amplified
            # across the remaining products.  The database is resumable.
            break

    if failures:
        logging.error("شناسه‌های متوقف‌شده: %s", ", ".join(map(str, failures)))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
