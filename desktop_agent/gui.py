from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable

from . import APP_NAME, APP_VERSION
from .config import load_settings
from .runner import ExtractionRunner


class DesktopAgentApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("980x720")
        self.root.minsize(760, 560)
        self.root.configure(bg="#f5f7fb")
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self.runner = ExtractionRunner(listener=self.events.put)
        self.settings = load_settings()
        self.job_options: list[dict[str, Any]] = []
        self.project_options: list[dict[str, Any]] = []
        self.vars = {key: tk.StringVar(value=value) for key, value in {
            "connection": "آفلاین", "device": str(self.settings.get("device_name") or ""), "version": APP_VERSION,
            "account": "—", "project": "—", "product": "—", "extracted": "۰", "synced": "۰", "queue": "۰",
            "last_sync": "—", "robot": "آماده", "errors": "بدون خطای اخیر", "message": ""
        }.items()}
        self.pairing_var = tk.StringVar()
        self.project_var = tk.StringVar(value="همه پروژه‌ها")
        self.job_var = tk.StringVar()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(200, self._drain_events)
        self.root.after(3000, self._poll)
        self.runner._notify()

    def _build(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 20, "bold"), foreground="#101828", background="#f5f7fb")
        style.configure("Muted.TLabel", font=("Segoe UI", 9), foreground="#667085", background="#f5f7fb")
        style.configure("Card.TFrame", background="#ffffff", relief="solid", borderwidth=1)
        style.configure("Card.TLabel", background="#ffffff", foreground="#101828")
        style.configure("Value.TLabel", background="#ffffff", foreground="#101828", font=("Segoe UI", 12, "bold"))
        style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"))

        outer = ttk.Frame(self.root, padding=24)
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 16))
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(side="right")
        ttk.Label(header, text=f"نسخه {APP_VERSION} · اجرای استخراج روی همین دستگاه", style="Muted.TLabel").pack(side="left", pady=8)

        connection = ttk.Frame(outer, style="Card.TFrame", padding=16)
        connection.pack(fill="x", pady=(0, 14))
        ttk.Label(connection, text="اتصال به سایت", style="Card.TLabel", font=("Segoe UI", 12, "bold")).grid(row=0, column=3, sticky="e", padx=8, pady=4)
        ttk.Label(connection, text="کد Pairing را از پنل مدیریت دریافت کنید:", style="Card.TLabel").grid(row=1, column=3, sticky="e", padx=8, pady=8)
        pairing = ttk.Entry(connection, textvariable=self.pairing_var, width=20, justify="center")
        pairing.grid(row=1, column=2, sticky="e", padx=8, pady=8)
        ttk.Button(connection, text="اتصال به سایت", style="Primary.TButton", command=self.connect).grid(row=1, column=1, sticky="e", padx=8, pady=8)
        ttk.Label(connection, textvariable=self.vars["connection"], style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=8, pady=8)
        ttk.Label(connection, textvariable=self.vars["message"], style="Card.TLabel", foreground="#9a6700").grid(row=2, column=0, columnspan=4, sticky="e", padx=8, pady=(2, 0))
        connection.columnconfigure(0, weight=1)

        self._build_summary(outer)

        work = ttk.Frame(outer)
        work.pack(fill="x", pady=(14, 0))
        job_card = ttk.Frame(work, style="Card.TFrame", padding=16)
        job_card.pack(side="right", fill="both", expand=True, padx=(8, 0))
        ttk.Label(job_card, text="Job پروژه", style="Card.TLabel", font=("Segoe UI", 12, "bold")).pack(anchor="e")
        ttk.Label(job_card, text="پروژه را انتخاب کنید؛ سپس Job صف‌شده را اجرا کنید.", style="Card.TLabel").pack(anchor="e", pady=(4, 8))
        self.project_combo = ttk.Combobox(job_card, textvariable=self.project_var, state="readonly", justify="right")
        self.project_combo.pack(fill="x", pady=4)
        ttk.Label(job_card, text="Job استخراج", style="Muted.TLabel").pack(anchor="e", pady=(8, 0))
        self.job_combo = ttk.Combobox(job_card, textvariable=self.job_var, state="readonly", justify="right")
        self.job_combo.pack(fill="x", pady=4)
        buttons = ttk.Frame(job_card, style="Card.TFrame")
        buttons.pack(fill="x", pady=(14, 0))
        for label, callback in [("شروع استخراج", self.start), ("Pause", self.pause), ("Resume", self.resume), ("Stop", self.stop), ("Retry Sync", self.retry_sync)]:
            ttk.Button(buttons, text=label, command=callback).pack(side="right", padx=3)

        settings_card = ttk.Frame(work, style="Card.TFrame", padding=16)
        settings_card.pack(side="left", fill="both", expand=True, padx=(0, 8))
        ttk.Label(settings_card, text="تنظیمات محلی", style="Card.TLabel", font=("Segoe UI", 12, "bold")).pack(anchor="e")
        ttk.Label(settings_card, text="آدرس Backend و نام دستگاه را کنترل کنید.", style="Card.TLabel").pack(anchor="e", pady=(4, 12))
        ttk.Button(settings_card, text="Settings", command=self.settings_dialog).pack(anchor="e")
        ttk.Label(settings_card, textvariable=self.vars["device"], style="Card.TLabel", wraplength=320).pack(anchor="e", pady=(14, 0))
        ttk.Label(settings_card, textvariable=self.vars["version"], style="Muted.TLabel").pack(anchor="e", pady=4)

        error_frame = ttk.Frame(outer, style="Card.TFrame", padding=16)
        error_frame.pack(fill="both", expand=True, pady=(14, 0))
        ttk.Label(error_frame, text="خطاهای اخیر", style="Card.TLabel", font=("Segoe UI", 12, "bold")).pack(anchor="e")
        self.error_text = tk.Text(error_frame, height=7, state="disabled", wrap="word", bg="#fffafa", fg="#8b1e1e", relief="flat")
        self.error_text.pack(fill="both", expand=True, pady=(8, 0))

    def _build_summary(self, outer: ttk.Frame) -> None:
        summary = ttk.Frame(outer)
        summary.pack(fill="x")
        fields = [("وضعیت اتصال", "connection"), ("دستگاه", "device"), ("حساب", "account"), ("پروژه", "project"), ("محصول", "product"), ("ربات", "robot"), ("استخراج‌شده", "extracted"), ("ارسال‌شده", "synced"), ("صف ارسال", "queue"), ("آخرین Sync", "last_sync")]
        for index, (title, key) in enumerate(fields):
            card = ttk.Frame(summary, style="Card.TFrame", padding=12)
            card.grid(row=index // 5, column=index % 5, sticky="nsew", padx=4, pady=4)
            ttk.Label(card, text=title, style="Muted.TLabel").pack(anchor="e")
            ttk.Label(card, textvariable=self.vars[key], style="Value.TLabel", wraplength=170).pack(anchor="e", pady=(5, 0))
        for column in range(5):
            summary.columnconfigure(column, weight=1)

    def _run_background(self, function: Callable[[], None]) -> None:
        threading.Thread(target=function, daemon=True).start()

    def connect(self) -> None:
        code = self.pairing_var.get().strip()
        self.vars["message"].set("در حال اتصال…")
        self._run_background(lambda: self._connect_worker(code))

    def _connect_worker(self, code: str) -> None:
        try:
            self.runner.connect(code)
            self.root.after(0, lambda: self.pairing_var.set(""))
        except Exception:
            pass

    def _selected_job(self) -> dict[str, Any] | None:
        index = self.job_combo.current()
        return self.job_options[index] if 0 <= index < len(self.job_options) else None

    def _selected_project_id(self) -> int | None:
        index = self.project_combo.current()
        if 0 <= index < len(self.project_options):
            return int(self.project_options[index].get("id"))
        return None

    def start(self) -> None:
        job = self._selected_job()
        if job:
            self.runner.start_job(job)
        else:
            self.vars["message"].set("ابتدا یک Job معتبر را انتخاب کنید.")

    def pause(self) -> None:
        self.runner.pause()

    def resume(self) -> None:
        self.runner.resume_current()

    def stop(self) -> None:
        self.runner.stop()

    def retry_sync(self) -> None:
        self.runner.retry_sync_async()

    def settings_dialog(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Settings")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        api_var = tk.StringVar(value=str(self.runner.settings.get("api_base_url") or ""))
        name_var = tk.StringVar(value=str(self.runner.settings.get("device_name") or ""))
        delay_var = tk.StringVar(value=str(self.runner.settings.get("bot_delay_seconds", 1.0)))
        batch_size_var = tk.StringVar(value=str(self.runner.settings.get("batch_size", 50)))
        batch_interval_var = tk.StringVar(value=str(self.runner.settings.get("batch_interval_seconds", 10.0)))
        for row, (label, variable) in enumerate([
            ("Backend URL", api_var),
            ("نام دستگاه", name_var),
            ("فاصله درخواست (ثانیه)", delay_var),
            ("اندازه Batch", batch_size_var),
            ("حداکثر فاصله ارسال (ثانیه)", batch_interval_var),
        ]):
            ttk.Label(frame, text=label).grid(row=row, column=1, sticky="e", padx=6, pady=7)
            ttk.Entry(frame, textvariable=variable, width=42, justify="left").grid(row=row, column=0, sticky="ew", padx=6, pady=7)
        def save() -> None:
            try:
                delay = max(0.2, float(delay_var.get()))
                batch_size = max(1, min(100, int(batch_size_var.get())))
                batch_interval = max(1.0, min(60.0, float(batch_interval_var.get())))
                self.runner.update_settings({
                    "api_base_url": api_var.get().strip().rstrip("/"),
                    "device_name": name_var.get().strip()[:160],
                    "bot_delay_seconds": delay,
                    "batch_size": batch_size,
                    "batch_interval_seconds": batch_interval,
                })
                dialog.destroy()
            except ValueError:
                messagebox.showerror("خطا", "مقدار تنظیمات معتبر نیست", parent=dialog)
        ttk.Button(frame, text="ذخیره", command=save).grid(row=5, column=0, columnspan=2, pady=(14, 0))

    def _drain_events(self) -> None:
        latest: dict[str, Any] | None = None
        while True:
            try:
                latest = self.events.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            self._render(latest)
        self.root.after(200, self._drain_events)

    def _render(self, state: dict[str, Any]) -> None:
        connection = "متصل" if state.get("connected") else "آفلاین"
        agent = state.get("agent") or {}
        if agent.get("status") == "REVOKED":
            connection = "دسترسی لغوشده"
        self.vars["connection"].set(connection)
        self.vars["device"].set(str(state.get("device_name") or "—"))
        self.vars["version"].set(f"نسخه {state.get('version') or APP_VERSION}")
        self.vars["account"].set(str(state.get("account") or "—"))
        self.vars["project"].set(str(state.get("project_name") or "—"))
        self.vars["product"].set(str(state.get("product_name") or "—"))
        self.vars["robot"].set(str(state.get("status") or "آماده"))
        self.vars["extracted"].set(f"{int(state.get('extracted', 0)):,}")
        self.vars["synced"].set(f"{int(state.get('synced', 0)):,}")
        self.vars["queue"].set(f"{int(state.get('sync_queue', 0)):,}")
        self.vars["last_sync"].set(str(state.get("last_sync_at") or "—"))
        self.vars["message"].set(str(state.get("last_error") or ""))
        jobs = state.get("jobs") or []
        current_id = (state.get("current_job") or {}).get("id")
        self.project_options = list(state.get("projects") or [])
        project_labels = [str(project.get("name") or "پروژه") for project in self.project_options]
        self.project_combo["values"] = project_labels
        current_project_id = (state.get("current_job") or {}).get("projectId")
        if current_project_id is not None:
            for index, project in enumerate(self.project_options):
                if int(project.get("id", 0)) == int(current_project_id):
                    self.project_combo.current(index)
                    break
        selected_project_id = self._selected_project_id()
        self.job_options = [
            job for job in jobs
            if (selected_project_id is None or int(job.get("projectId") or 0) == selected_project_id)
            and (job.get("status") in {"QUEUED", "PAUSED", "RUNNING", "CLAIMED"} or job.get("id") == current_id)
        ]
        labels = [f"#{job.get('id')} · {job.get('productName') or 'محصول'} · {job.get('prefix')} · {job.get('status')}" for job in self.job_options]
        self.job_combo["values"] = labels
        if current_id:
            for index, job in enumerate(self.job_options):
                if job.get("id") == current_id:
                    self.job_combo.current(index)
                    break
        errors = state.get("recent_errors") or []
        self.error_text.configure(state="normal")
        self.error_text.delete("1.0", "end")
        self.error_text.insert("1.0", "\n".join(str(item) for item in errors) or "بدون خطای اخیر")
        self.error_text.configure(state="disabled")

    def _poll(self) -> None:
        self.runner.refresh_async()
        self.root.after(5000, self._poll)

    def close(self) -> None:
        self.runner.close()
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    DesktopAgentApp(root)
    root.mainloop()
    return 0
