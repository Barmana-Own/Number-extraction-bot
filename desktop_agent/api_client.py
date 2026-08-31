from __future__ import annotations

import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class AgentApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class ControlPlaneClient:
    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = max(3.0, float(timeout))

    def set_token(self, token: str | None) -> None:
        self.token = token

    def _validate_base_url(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise AgentApiError("آدرس Backend معتبر نیست")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise AgentApiError("آدرس Backend نباید credential، query یا fragment داشته باشد")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise AgentApiError("برای اتصال غیرمحلی باید HTTPS استفاده شود")

    def request(self, path: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
        self._validate_base_url()
        if not path.startswith("/"):
            path = "/" + path
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.token:
            headers["X-Hamshomareh-Device-Token"] = self.token
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read(2_000_000)
                status = int(response.status)
        except HTTPError as error:
            raw = error.read(100_000)
            message = self._safe_error_message(raw, int(error.code))
            raise AgentApiError(message, status=int(error.code)) from None
        except (URLError, TimeoutError, OSError) as error:
            raise AgentApiError(f"ارتباط با Backend برقرار نشد: {error}") from None
        try:
            decoded = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise AgentApiError(f"پاسخ Backend معتبر نیست (HTTP {status})", status=status) from None
        if status >= 400:
            raise AgentApiError(self._safe_error_message(raw, status), status=status)
        return decoded

    @staticmethod
    def _safe_error_message(raw: bytes, status: int) -> str:
        try:
            decoded = json.loads(raw.decode("utf-8"))
            message = str(decoded.get("message") or "") if isinstance(decoded, dict) else ""
        except (UnicodeDecodeError, json.JSONDecodeError):
            message = ""
        message = re.sub(r"[\r\n\t]+", " ", message).strip()
        return message[:300] or f"درخواست رد شد (HTTP {status})"

    def pair(self, *, code: str, device_id: str, device_name: str, version: str) -> dict[str, Any]:
        return self.request("/desktop/pair", method="POST", payload={
            "code": code, "deviceId": device_id, "deviceName": device_name, "version": version
        })

    def status(self) -> dict[str, Any]:
        return self.request("/desktop/status")

    def projects(self) -> list[dict[str, Any]]:
        return list(self.request("/desktop/projects").get("projects") or [])

    def jobs(self) -> list[dict[str, Any]]:
        return list(self.request("/desktop/jobs").get("jobs") or [])

    def heartbeat(self, job_id: int | None = None, version: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if job_id is not None:
            payload["jobId"] = job_id
        if version:
            payload["version"] = version
        return self.request("/desktop/heartbeat", method="POST", payload=payload)

    def claim(self, job_id: int) -> dict[str, Any]:
        return self.request(f"/desktop/jobs/{job_id}/claim", method="POST")

    def start(self, job_id: int) -> dict[str, Any]:
        return self.request(f"/desktop/jobs/{job_id}/start", method="POST")

    def progress(self, job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request(f"/desktop/jobs/{job_id}/progress", method="POST", payload=payload)

    def numbers(self, job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request(f"/desktop/jobs/{job_id}/numbers", method="POST", payload=payload)

    def complete(self, job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request(f"/desktop/jobs/{job_id}/complete", method="POST", payload=payload)

    def fail(self, job_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        return self.request(f"/desktop/jobs/{job_id}/fail", method="POST", payload=payload)
