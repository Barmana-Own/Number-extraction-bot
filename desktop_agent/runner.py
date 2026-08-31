from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Callable

from irancell_number_bot import ApiClient as IrancellApiClient
from irancell_number_bot import ProductConfig, extract_product, load_products, local_number_count, normalize_bot_prefix

from . import APP_VERSION
from .api_client import AgentApiError, ControlPlaneClient
from .config import DATA_DIR, LOG_DIR, OUTPUT_DIR, SETTINGS_FILE, STATE_DB, TOKEN_FILE, load_settings, save_settings
from .secure_store import delete_token, load_token, save_token
from .storage import AgentStorage

StateListener = Callable[[dict[str, Any]], None]


def _slug(value: str) -> str:
    result = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return result[:80] or "product"


def _product_slug(product_id: int, product_name: str) -> str:
    """Keep the bot's established directory names stable across the API integration."""
    try:
        configured = next((product for product in load_products() if product.id == product_id), None)
    except (OSError, TypeError, ValueError):
        configured = None
    return configured.slug if configured else _slug(product_name)


class ExtractionRunner:
    def __init__(self, listener: StateListener | None = None) -> None:
        self.settings = load_settings()
        self.listener = listener
        self.storage = AgentStorage(STATE_DB)
        self.client = ControlPlaneClient(
            str(self.settings.get("api_base_url") or ""),
            token=load_token(TOKEN_FILE),
            timeout=float(self.settings.get("request_timeout_seconds", 45.0)),
        )
        self.lock = threading.RLock()
        self.worker: threading.Thread | None = None
        self.heartbeat_worker: threading.Thread | None = None
        self.heartbeat_stop = threading.Event()
        self.pause_requested = threading.Event()
        self.stop_requested = threading.Event()
        self.shutdown_requested = threading.Event()
        self.refresh_worker: threading.Thread | None = None
        self.current_job: dict[str, Any] | None = None
        self.stats: dict[str, Any] = {}
        self.last_sync_at: str | None = None
        self.last_error: str | None = None
        self.errors: deque[str] = deque(maxlen=8)
        self.connected = bool(self.client.token)
        self.agent: dict[str, Any] = {}
        self.projects: list[dict[str, Any]] = []
        self.jobs: list[dict[str, Any]] = []
        self._pending_numbers: list[str] = []
        self._last_progress_at = 0.0
        self._last_batch_at = 0.0
        self._auto_resume_job_id: int | None = None
        self.closed = False

    def _record_error(self, message: str) -> None:
        safe = str(message).replace("\r", " ").replace("\n", " ")[:500]
        with self.lock:
            self.last_error = safe
            self.errors.appendleft(safe)
        logging.error("Desktop Agent: %s", safe)
        self._notify()

    def _notify(self) -> None:
        if self.listener is None:
            return
        try:
            self.listener(self.snapshot())
        except Exception:
            logging.debug("state listener failed", exc_info=True)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            current = dict(self.current_job or {})
            stats = dict(self.stats)
            return {
                "connected": self.connected,
                "api_base_url": str(self.settings.get("api_base_url") or ""),
                "device_name": str(self.settings.get("device_name") or ""),
                "device_id": str(self.settings.get("device_id") or ""),
                "version": APP_VERSION,
                "account": self.agent.get("userId") or self.agent.get("user_id"),
                "agent": dict(self.agent),
                "projects": list(self.projects),
                "jobs": list(self.jobs),
                "current_job": current,
                "project_name": current.get("projectName") or current.get("project_name") or "",
                "product_name": current.get("productName") or current.get("product_name") or "",
                "status": current.get("status") or ("connected" if self.connected else "offline"),
                "extracted": int(stats.get("extracted", current.get("fetched", 0)) or 0),
                "synced": int(stats.get("synced", current.get("imported", 0)) or 0),
                "duplicates": int(stats.get("duplicates", current.get("duplicates", 0)) or 0),
                "requests": int(stats.get("requests", current.get("requests", 0)) or 0),
                "patterns_scanned": int(stats.get("patterns_scanned", current.get("patternsScanned", 0)) or 0),
                "pending_patterns": int(stats.get("pending_patterns", current.get("pendingPatterns", 0)) or 0),
                "retries": int(stats.get("retries", current.get("retries", 0)) or 0),
                "sync_queue": self.storage.pending_count(),
                "last_sync_at": self.last_sync_at,
                "last_error": self.last_error,
                "recent_errors": list(self.errors),
                "busy": self.worker is not None and self.worker.is_alive(),
            }

    def update_settings(self, values: dict[str, Any]) -> None:
        with self.lock:
            self.settings.update(values)
            self.client.base_url = str(self.settings.get("api_base_url") or "").rstrip("/")
            self.client.timeout = max(3.0, float(self.settings.get("request_timeout_seconds", 45.0)))
        save_settings(self.settings)
        self._notify()

    def connect(self, pairing_code: str = "") -> None:
        try:
            if pairing_code.strip():
                result = self.client.pair(
                    code=pairing_code.strip(),
                    device_id=str(self.settings["device_id"]),
                    device_name=str(self.settings.get("device_name") or "Hamshmareh Desktop"),
                    version=APP_VERSION,
                )
                token = str(result.get("token") or "")
                if not token:
                    raise AgentApiError("Backend توکن Desktop صادر نکرد")
                save_token(TOKEN_FILE, token)
                self.client.set_token(token)
            if not self.client.token:
                raise AgentApiError("کد اتصال را وارد کنید")
            if not self.refresh_remote(auto_resume=True):
                raise AgentApiError("اتصال به Backend برقرار نشد؛ آدرس API و دسترسی شبکه را بررسی کنید")
            with self.lock:
                self.connected = True
            self._notify()
        except Exception as error:
            with self.lock:
                self.connected = False
            self._record_error(error)
            raise

    def disconnect(self) -> None:
        self.storage.set_meta("resume_on_restart", False)
        self.client.set_token(None)
        delete_token(TOKEN_FILE)
        with self.lock:
            self.connected = False
            self.agent = {}
            self.projects = []
            self.jobs = []
        self._notify()

    def refresh_remote(self, *, auto_resume: bool = False) -> bool:
        if self.closed or not self.client.token:
            return False
        try:
            status = self.client.status()
            projects = self.client.projects()
            jobs = self.client.jobs()
            with self.lock:
                worker_alive = bool(self.worker and self.worker.is_alive())
                self.agent = status.get("agent") or {}
                self.projects = projects
                self.jobs = jobs
                self.connected = True
                active = status.get("activeJob")
                current_id = int(self.current_job.get("id", 0)) if self.current_job else 0
                latest = next((job for job in jobs if int(job.get("id", 0)) == current_id), None)
                if active and not worker_alive:
                    self.current_job = active
                elif latest and not worker_alive:
                    self.current_job = latest
                running = next(
                    (job for job in jobs if job.get("agentId") == self.agent.get("id") and job.get("status") in {"CLAIMED", "RUNNING"}),
                    None,
                )
                restart_job_id = self.storage.get_meta("last_job_id")
                resume_on_restart = bool(self.storage.get_meta("resume_on_restart", False))
                paused = next(
                    (job for job in jobs if restart_job_id and int(job.get("id", 0)) == int(restart_job_id) and job.get("status") == "PAUSED"),
                    None,
                )
            candidate = running or (paused if resume_on_restart else None)
            if auto_resume and candidate and self._auto_resume_job_id != int(candidate["id"]):
                self._auto_resume_job_id = int(candidate["id"])
                self.start_job(candidate)
            self._notify()
            return True
        except AgentApiError as error:
            with self.lock:
                self.connected = False
            self._record_error(error)
            return False
        except Exception as error:
            self._record_error(error)
            return False

    def refresh_async(self, *, auto_resume: bool = False) -> None:
        with self.lock:
            if self.closed or (self.refresh_worker and self.refresh_worker.is_alive()):
                return
            self.refresh_worker = threading.Thread(target=self.refresh_remote, kwargs={"auto_resume": auto_resume}, daemon=True)
            self.refresh_worker.start()

    def start_job(self, job: dict[str, Any]) -> None:
        with self.lock:
            if self.worker and self.worker.is_alive():
                self._record_error("یک Job دیگر در حال اجرا است")
                return
            self.pause_requested.clear()
            self.stop_requested.clear()
            self.current_job = dict(job)
            self.stats = self.storage.load_checkpoint(int(job["id"]))
            self._pending_numbers = []
            self.storage.set_meta("last_job_id", int(job["id"]))
            self.storage.set_meta("resume_on_restart", True)
            self.worker = threading.Thread(target=self._run_job, args=(dict(job),), name="desktop-extraction", daemon=True)
            self.worker.start()
        self._notify()

    def pause(self) -> None:
        self.pause_requested.set()
        self.storage.set_meta("resume_on_restart", False)
        self._notify()

    def stop(self) -> None:
        self.stop_requested.set()
        self.storage.set_meta("resume_on_restart", False)
        self._notify()

    def resume_current(self) -> None:
        with self.lock:
            job = dict(self.current_job or {})
        if job:
            self.start_job(job)

    def retry_sync_async(self) -> None:
        threading.Thread(target=self._retry_sync_worker, name="desktop-sync", daemon=True).start()

    def _retry_sync_worker(self) -> None:
        if not self.client.token:
            self._record_error("ابتدا Desktop Agent را به سایت متصل کنید")
            return
        self.storage.retry_batches_now()
        try:
            with self.lock:
                active_job_id = int(self.current_job.get("id", 0)) if self.current_job else 0
                worker_alive = bool(self.worker and self.worker.is_alive())
            if worker_alive and active_job_id:
                self._flush_outbox(active_job_id)
                return
            self.refresh_remote(auto_resume=False)
            remote_jobs = {int(job.get("id")): job for job in self.client.jobs() if job.get("id") is not None}
            pending_rows = self.storage.due_batches(limit=20)
            for row in pending_rows:
                job_id = int(row["job_id"])
                job = remote_jobs.get(job_id)
                if not job:
                    self._record_error(f"Job #{job_id} در سایت پیدا نشد؛ صف محلی حفظ شد")
                    continue
                agent_id = (self.agent or {}).get("id")
                assigned_agent = job.get("agentId")
                if assigned_agent not in (None, agent_id):
                    self._record_error(f"Job #{job_id} به Desktop دیگری واگذار شده است؛ صف محلی حفظ شد")
                    continue
                if job.get("status") not in {"QUEUED", "CLAIMED", "RUNNING", "PAUSED"}:
                    self._record_error(f"Job #{job_id} پایان یافته است؛ برای Sync دوباره آن را از پنل Retry کنید")
                    continue
                self.client.claim(job_id)
                self.client.start(job_id)
                self._flush_outbox(job_id)
            self.refresh_async()
        except AgentApiError as error:
            self._record_error(error)
            if error.status == 401:
                with self.lock:
                    self.connected = False
        except Exception as error:
            self._record_error(error)

    def _decision(self) -> str | bool:
        if self.stop_requested.is_set():
            return "stop"
        if self.pause_requested.is_set():
            return "pause"
        return False

    def _start_heartbeat(self, job_id: int) -> None:
        self.heartbeat_stop.clear()
        self.heartbeat_worker = threading.Thread(target=self._heartbeat_loop, args=(job_id,), name="desktop-heartbeat", daemon=True)
        self.heartbeat_worker.start()

    def _stop_heartbeat(self) -> None:
        self.heartbeat_stop.set()
        worker = self.heartbeat_worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout=2.0)
        self.heartbeat_worker = None

    def _heartbeat_loop(self, job_id: int) -> None:
        while not self.heartbeat_stop.wait(15.0):
            try:
                response = self.client.heartbeat(job_id, APP_VERSION)
                if response.get("cancelRequested"):
                    self.stop_requested.set()
                    self._record_error("لغو Job از پنل مدیریت دریافت شد")
            except Exception as error:
                self._record_error(error)

    def _update_stats(self, **values: Any) -> None:
        with self.lock:
            self.stats.update(values)
            if self.current_job is not None:
                self.storage.save_checkpoint(int(self.current_job["id"]), self.stats)

    def _send_progress(self, job_id: int, *, force: bool = False) -> None:
        if not self.client.token:
            return
        now = time.monotonic()
        if not force and now - self._last_progress_at < 5.0:
            return
        with self.lock:
            stats = dict(self.stats)
        payload = {
            "requests": int(stats.get("requests", 0)), "scannedPatterns": int(stats.get("patterns_scanned", 0)),
            "pendingPatterns": int(stats.get("pending_patterns", 0)), "uniqueNumbers": int(stats.get("extracted", 0)),
            "syncedNumbers": int(stats.get("synced", 0)), "duplicates": int(stats.get("duplicates", 0)),
            "retries": int(stats.get("retries", 0)), "currentPattern": stats.get("current_pattern"),
            "lastError": self.last_error,
        }
        try:
            self.client.progress(job_id, payload)
            self._last_progress_at = now
            self.last_sync_at = dt_now()
        except Exception as error:
            self._record_error(error)
        self._notify()

    def _queue_number_batch(self, job_id: int, *, pattern: str | None) -> None:
        batch_size = int(self.settings.get("batch_size", 50))
        rows = self.storage.pending_numbers(job_id, limit=batch_size)
        if not rows:
            return
        numbers = [str(row["number"]) for row in rows]
        batch_pattern = pattern or (str(rows[0]["pattern"]) if rows[0]["pattern"] else None)
        with self.lock:
            stats = dict(self.stats)
        batch_id = f"job-{job_id}-{uuid.uuid4().hex}"
        payload = {
            "batchId": batch_id,
            "numbers": numbers,
            "pattern": batch_pattern,
            "requests": int(stats.get("requests", 0)),
            "scannedPatterns": int(stats.get("patterns_scanned", 0)),
            "pendingPatterns": int(stats.get("pending_patterns", 0)),
            "uniqueNumbers": int(stats.get("extracted", 0)),
            "retries": int(stats.get("retries", 0)),
        }
        self.storage.enqueue_batch(batch_id, job_id, payload)
        self.storage.remove_pending_numbers(job_id, numbers)
        remaining = set(numbers)
        self._pending_numbers = [number for number in self._pending_numbers if number not in remaining]
        self._last_batch_at = time.monotonic()
        self._flush_outbox(job_id)

    def _flush_outbox(self, job_id: int | None = None) -> bool:
        if not self.client.token:
            self._notify()
            return False
        for row in self.storage.due_batches(limit=10, job_id=job_id):
            batch_id = str(row["batch_id"])
            try:
                import json
                payload = json.loads(row["payload_json"])
                result = self.client.numbers(int(row["job_id"]), payload)
                self.storage.mark_batch_sent(batch_id)
                current_job_id = int(self.current_job["id"]) if self.current_job else None
                if not result.get("replayed") and current_job_id == int(row["job_id"]):
                    self._update_stats(
                        synced=int(self.stats.get("synced", 0)) + int(result.get("imported", 0) or 0),
                        duplicates=int(self.stats.get("duplicates", 0)) + int(result.get("duplicates", 0) or 0),
                    )
                self.last_sync_at = dt_now()
            except AgentApiError as error:
                attempts = int(row["attempts"] or 0) + 1
                self.storage.mark_batch_failed(batch_id, str(error), attempts)
                self._record_error(error)
                if error.status == 401:
                    with self.lock:
                        self.connected = False
                break
            except Exception as error:
                attempts = int(row["attempts"] or 0) + 1
                self.storage.mark_batch_failed(batch_id, str(error), attempts)
                self._record_error(error)
                break
        self._notify()
        return self.storage.pending_count(job_id) == 0

    def _bot_event(self, event: str, payload: dict[str, Any]) -> None:
        if event == "on_number_found":
            job_id = int(self.current_job["id"]) if self.current_job else 0
            number = str(payload.get("number") or "").strip()
            if not job_id or not number:
                return
            self.storage.add_pending_number(job_id, number, str(payload.get("pattern") or "") or None)
            self._pending_numbers.append(number)
            batch_size = max(1, int(self.settings.get("batch_size", 50)))
            batch_interval = max(1.0, float(self.settings.get("batch_interval_seconds", 10.0)))
            if len(self._pending_numbers) >= batch_size or time.monotonic() - self._last_batch_at >= batch_interval:
                self._queue_number_batch(job_id, pattern=payload.get("pattern"))
        elif event == "on_progress":
            self._update_stats(
                requests=int(payload.get("requests", 0)), patterns_scanned=int(payload.get("scanned_patterns", 0)),
                pending_patterns=int(payload.get("pending_patterns", 0)), extracted=int(payload.get("unique_numbers", 0)),
                current_pattern=payload.get("current_pattern"),
            )
            if self.current_job:
                self._send_progress(int(self.current_job["id"]))
        elif event == "on_rate_limited":
            self._update_stats(retries=int(self.stats.get("retries", 0)) + 1)
            self._record_error(f"محدودیت 429؛ مکث {payload.get('retry_in_seconds', 0)} ثانیه")
        elif event == "on_error":
            message = str(payload.get("message") or "خطای استخراج")
            if payload.get("retryable"):
                self._update_stats(retries=int(self.stats.get("retries", 0)) + 1)
            self._record_error(message)
        elif event == "on_product_started":
            self._notify()

    def _finish_payload(self, status: str, error: str | None = None) -> dict[str, Any]:
        with self.lock:
            stats = dict(self.stats)
        return {
            "status": status,
            "requests": int(stats.get("requests", 0)), "scannedPatterns": int(stats.get("patterns_scanned", 0)),
            "pendingPatterns": int(stats.get("pending_patterns", 0)), "uniqueNumbers": int(stats.get("extracted", 0)),
            "syncedNumbers": int(stats.get("synced", 0)), "duplicates": int(stats.get("duplicates", 0)),
            "retries": int(stats.get("retries", 0)), "currentPattern": stats.get("current_pattern"),
            "error": error or self.last_error,
        }

    def _run_job(self, source_job: dict[str, Any]) -> None:
        job_id = int(source_job["id"])
        failure: str | None = None
        final_status: str | None = None
        completion_failed = False
        try:
            claimed = self.client.claim(job_id)
            started = self.client.start(job_id)
            with self.lock:
                self.current_job = started or claimed or source_job
                self.connected = True
            self._start_heartbeat(job_id)
            self._queue_number_batch(job_id, pattern=None)
            product = ProductConfig(
                int(self.current_job.get("productId") or self.current_job.get("product_id")),
                _product_slug(
                    int(self.current_job.get("productId") or self.current_job.get("product_id")),
                    str(self.current_job.get("productName") or self.current_job.get("product_name") or job_id),
                ),
                str(self.current_job.get("productName") or self.current_job.get("product_name") or "IranCell"),
            )
            prefix_value = str(self.current_job.get("prefix") or self.current_job.get("prefix_value") or "")
            baseline = self.stats.get("baseline_numbers")
            if not isinstance(baseline, int) or baseline < 0:
                baseline = local_number_count(OUTPUT_DIR, product, normalize_bot_prefix(prefix_value) if prefix_value else None)
                self._update_stats(baseline_numbers=baseline)
            target_count = int(self.current_job.get("targetCount") or self.current_job.get("target_count") or 0)
            absolute_target = baseline + target_count if target_count > 0 else 0
            max_requests = int(self.current_job.get("maxRequests") or self.current_job.get("max_requests") or 0)
            requests_done = int(self.stats.get("requests", 0) or 0)
            remaining_requests = max(0, max_requests - requests_done) if max_requests else 0

            def should_stop() -> str | bool:
                decision = self._decision()
                if decision:
                    return decision
                if max_requests and requests_done >= max_requests:
                    return "pause"
                return False

            bot_client = IrancellApiClient(
                delay=float(self.settings.get("bot_delay_seconds", 1.0)),
                max_retries=int(self.settings.get("bot_max_retries", 6)),
                retry_forever_on_429=True,
                rate_limit_cooldown=float(self.settings.get("bot_rate_limit_cooldown_seconds", 600.0)),
                timeout=float(self.settings.get("request_timeout_seconds", 45.0)),
                event_callback=self._bot_event,
            )
            result = extract_product(
                bot_client,
                product,
                output_root=OUTPUT_DIR,
                max_requests=max(1, remaining_requests) if max_requests else 0,
                prefix_filter=prefix_value,
                start_tail=str(self.current_job.get("tail") or self.current_job.get("requested_tail") or ""),
                max_numbers=absolute_target,
                should_stop=should_stop,
                on_event=self._bot_event,
                baseline_number_count=baseline,
                request_offset=requests_done,
            )
            self._update_stats(
                requests=int(result.get("requests", self.stats.get("requests", 0))),
                patterns_scanned=int(result.get("scanned_patterns", self.stats.get("patterns_scanned", 0))),
                pending_patterns=int(result.get("pending_patterns", self.stats.get("pending_patterns", 0))),
                extracted=int(result.get("new_number_count", self.stats.get("extracted", 0))),
            )
            self._queue_number_batch(job_id, pattern=self.stats.get("current_pattern"))
            sync_complete = self._flush_outbox(job_id)
            if self.stop_requested.is_set():
                final_status = "CANCELLED"
            elif self.pause_requested.is_set() or result.get("status") in {"paused_request_limit", "paused_by_user", "stopped"}:
                final_status = "PAUSED"
            else:
                final_status = "SUCCESS" if result.get("status") in {"complete", "target_reached", "skipped_no_number_selection"} else "PARTIAL"
            if final_status == "SUCCESS" and not sync_complete:
                final_status = "PAUSED"
                if not self.last_error:
                    self.last_error = "صف محلی شماره‌ها هنوز با سایت همگام نشده است"
            try:
                self.client.complete(job_id, self._finish_payload(final_status))
            except Exception as error:
                completion_failed = True
                self._record_error(error)
        except AgentApiError as error:
            failure = str(error)
            self._record_error(error)
            with self.lock:
                self.connected = False
            try:
                self.client.fail(job_id, self._finish_payload("FAILED", failure))
            except Exception:
                pass
        except Exception as error:
            failure = str(error)
            self._record_error(error)
            try:
                self.client.fail(job_id, self._finish_payload("FAILED", failure))
            except Exception as finish_error:
                self._record_error(finish_error)
        finally:
            self._stop_heartbeat()
            with self.lock:
                self.current_job = dict(self.current_job or source_job)
                if failure:
                    self.current_job["status"] = "FAILED"
                self.worker = None
            self.storage.set_meta(
                "resume_on_restart",
                bool(
                    not self.stop_requested.is_set()
                    and (
                        self.shutdown_requested.is_set()
                        or not self.pause_requested.is_set()
                    )
                    and (self.storage.pending_count(job_id) > 0 or completion_failed or failure is not None or final_status == "PAUSED")
                ),
            )
            self._notify()
            self.refresh_async()

    def close(self) -> None:
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.shutdown_requested.set()
            if self.worker and self.worker.is_alive():
                # Closing the window is a resumable pause. An explicit Stop
                # remains terminal because stop_requested is already set.
                self.pause_requested.set()
                self.storage.set_meta("resume_on_restart", True)
            else:
                self.storage.set_meta("resume_on_restart", False)
        self._stop_heartbeat()
        worker = self.worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout=5.0)
        if not worker or not worker.is_alive():
            self.storage.close()


def dt_now() -> str:
    import datetime as dt
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
