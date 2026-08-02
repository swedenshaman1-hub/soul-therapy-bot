"""Isolated, cloud-only NotebookLM connector used by the Telegram bot.

The connector intentionally runs every NotebookLM request in a short-lived
subprocess.  This mirrors the proven Railway setup used by the working
``Архитектор роста`` bot: a failed or stale NotebookLM singleton cannot poison
the long-running Telegram process.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx


logger = logging.getLogger(__name__)

_BUILD_LABEL_RE = re.compile(
    r"boq_labs-tailwind-frontend_[A-Za-z0-9._-]+_p\d+"
)
_NOTEBOOK_PAGE_URL = "https://notebook.google.com"
_HOST_SCOPED_COOKIES = frozenset({"OSID", "__Secure-OSID"})
_PAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


class NotebookConnectorError(RuntimeError):
    """NotebookLM credentials or responses are unavailable."""


class NotebookConnector:
    def __init__(self, notebook_id: str, timeout: int = 130):
        self.notebook_id = notebook_id
        self.timeout = timeout
        self._source_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._query_slots = threading.BoundedSemaphore(
            max(1, int(os.getenv("NOTEBOOKLM_PARALLEL_QUERIES", "2")))
        )
        self._chat_locks: dict[int, threading.Lock] = {}
        self._source_ids: list[str] = []
        self._conversations: dict[int, str] = {}
        self._last_error = ""
        self._auth = self._load_auth()
        self._persist_auth()
        if os.getenv("NOTEBOOKLM_AUTO_METADATA", "1") != "0":
            self._refresh_frontend_metadata()

    @staticmethod
    def _load_auth() -> dict:
        encoded = os.getenv("NOTEBOOKLM_AUTH_JSON_B64", "").strip()
        plain = os.getenv("NOTEBOOKLM_AUTH_JSON", "").strip()
        candidates: list[dict] = []
        env_auth_invalid = False

        try:
            if encoded:
                raw = base64.b64decode(encoded).decode("utf-8")
            elif plain:
                raw = plain
            else:
                raw = ""
            if raw:
                env_auth = json.loads(raw.lstrip("\ufeff"))
                if isinstance(env_auth, dict) and isinstance(
                    env_auth.get("cookies"), dict
                ):
                    candidates.append(env_auth)
                else:
                    env_auth_invalid = True
        except Exception:
            env_auth_invalid = True
            logger.warning(
                "Railway NotebookLM auth is invalid; trying persistent auth"
            )

        data_dir = os.getenv("NOTEBOOKLM_MCP_DATA_DIR", "").strip()
        if data_dir:
            disk_path = Path(data_dir) / "auth.json"
            if disk_path.exists():
                try:
                    disk_auth = json.loads(disk_path.read_text(encoding="utf-8"))
                    if isinstance(disk_auth.get("cookies"), dict):
                        candidates.append(disk_auth)
                except Exception as exc:
                    logger.warning("Ignored invalid persistent NotebookLM auth: %s", exc)

        if not candidates:
            if env_auth_invalid:
                raise NotebookConnectorError(
                    "Повреждена облачная авторизация NotebookLM"
                )
            raise NotebookConnectorError(
                "Не задана облачная авторизация NotebookLM"
            )

        # Metadata refreshes can rotate short-lived Google cookies.  Prefer the
        # newest persisted copy over the older Railway environment snapshot.
        auth = max(
            candidates,
            key=lambda item: float(item.get("extracted_at", 0) or 0),
        )
        cookies = auth.get("cookies")
        if not isinstance(cookies, dict) or not cookies:
            raise NotebookConnectorError(
                "В облачной авторизации NotebookLM отсутствуют cookies"
            )
        return auth

    def _persist_auth(self) -> None:
        data_dir = os.getenv("NOTEBOOKLM_MCP_DATA_DIR", "").strip()
        if not data_dir:
            return
        path = Path(data_dir)
        path.mkdir(parents=True, exist_ok=True)
        (path / "auth.json").write_text(
            json.dumps(self._auth, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def _is_auth_error(error: str) -> bool:
        lowered = error.lower()
        return any(marker in lowered for marker in (
            "401",
            "403",
            "not authenticated",
            "authentication expired",
            "auth expired",
            "rpc error 16",
        ))

    def _refresh_frontend_metadata(self) -> bool:
        """Refresh build label, CSRF and session metadata from Google."""
        jar = httpx.Cookies()
        for name, value in self._auth.get("cookies", {}).items():
            domain = "notebook.google.com" if name in _HOST_SCOPED_COOKIES else ".google.com"
            jar.set(name, value, domain=domain)

        try:
            with httpx.Client(
                cookies=jar,
                headers=_PAGE_HEADERS,
                follow_redirects=True,
                timeout=httpx.Timeout(60.0, connect=20.0),
            ) as client:
                response = client.get(f"{_NOTEBOOK_PAGE_URL}/")
                if "accounts.google.com" in str(response.url):
                    self._last_error = "NotebookLM authentication expired"
                    return False
                response.raise_for_status()
                html = response.text

                # The first navigation can return only the application shell.
                # A second request with the cookies set by that shell contains
                # the current frontend label used by NotebookLM RPC calls.
                label = _BUILD_LABEL_RE.search(html)
                if not label:
                    second = client.get(f"{_NOTEBOOK_PAGE_URL}/")
                    second.raise_for_status()
                    html = second.text
                    label = _BUILD_LABEL_RE.search(html)

                # Preserve any cookie rotation returned by Google.
                cookies = self._auth.setdefault("cookies", {})
                for cookie in client.cookies.jar:
                    cookies[cookie.name] = cookie.value

            if label:
                os.environ["NOTEBOOKLM_BL"] = label.group(0)

            from notebooklm_mcp_2026.auth import (
                extract_csrf_from_html,
                extract_session_id_from_html,
            )

            csrf = extract_csrf_from_html(html)
            if csrf:
                self._auth["csrf_token"] = csrf
            session_id = extract_session_id_from_html(html)
            if session_id:
                self._auth["session_id"] = session_id
            self._auth["extracted_at"] = time.time()
            self._persist_auth()
            logger.info(
                "NotebookLM metadata refreshed: build=%s csrf=%s",
                os.getenv("NOTEBOOKLM_BL", "default"),
                bool(csrf),
            )
            return bool(label or csrf)
        except Exception as exc:
            logger.warning("NotebookLM metadata refresh failed: %s", exc)
            return False

    def refresh_session(self) -> bool:
        """Keep the Google session active and persist rotated cookies."""
        return self._refresh_frontend_metadata()

    def _run_once(
        self,
        query: str,
        conversation_id: str | None,
        *,
        sources_only: bool = False,
        source_ids: list[str] | None = None,
    ) -> dict:
        script = r"""
import json
import os
import sys
import time

payload = json.load(sys.stdin)
build_label = payload.get("build_label")
if build_label:
    os.environ["NOTEBOOKLM_BL"] = build_label
base_url = (payload.get("base_url") or "").rstrip("/")
if base_url:
    os.environ["NOTEBOOKLM_BASE_URL"] = base_url
    from notebooklm_mcp_2026 import config
    config.BASE_URL = base_url
    config.BATCHEXECUTE_URL = f"{base_url}/_/LabsTailwindUi/data/batchexecute"

from notebooklm_mcp_2026 import server
from notebooklm_mcp_2026.client import NotebookLMClient, _extract_source_ids
from notebooklm_mcp_2026.tools.query import query_notebook

auth = payload["auth"]
server._client = NotebookLMClient(
    cookies=auth.get("cookies", {}),
    csrf_token=auth.get("csrf_token", ""),
    session_id=auth.get("session_id", ""),
)

source_ids = payload.get("source_ids") or []
source_started = time.monotonic()
if not source_ids:
    notebook = server._client.get_notebook(payload["notebook_id"])
    source_ids = _extract_source_ids(notebook)
source_seconds = round(time.monotonic() - source_started, 3)

if not source_ids:
    print(json.dumps({
        "status": "error",
        "error": "NotebookLM source list is empty",
        "_timings": {"sources": source_seconds},
    }, ensure_ascii=False))
    raise SystemExit(0)

if payload.get("sources_only"):
    print(json.dumps({
        "status": "success",
        "_source_ids": source_ids,
        "_timings": {"sources": source_seconds},
    }, ensure_ascii=False))
    raise SystemExit(0)

query_started = time.monotonic()
result = query_notebook(
    notebook_id=payload["notebook_id"],
    query=payload["query"],
    source_ids=source_ids,
    conversation_id=payload.get("conversation_id") or None,
)
result["_source_ids"] = source_ids
result["_timings"] = {
    "sources": source_seconds,
    "query": round(time.monotonic() - query_started, 3),
}
print(json.dumps(result, ensure_ascii=False))
"""
        payload = {
            "notebook_id": self.notebook_id,
            "query": query,
            "conversation_id": conversation_id,
            "auth": self._auth,
            "build_label": os.getenv("NOTEBOOKLM_BL", "").strip(),
            "base_url": os.getenv("NOTEBOOKLM_BASE_URL", _NOTEBOOK_PAGE_URL).strip(),
            "source_ids": list(source_ids or []),
            "sources_only": sources_only,
        }
        process_env = os.environ.copy()
        process_env["PYTHONIOENCODING"] = "utf-8"
        process_env["PYTHONUTF8"] = "1"
        try:
            proc = subprocess.run(
                [sys.executable, "-c", script],
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=self.timeout,
                env=process_env,
            )
        except subprocess.TimeoutExpired:
            return {
                "status": "error",
                "error": f"NotebookLM timeout after {self.timeout}s",
            }

        if proc.returncode != 0:
            return {
                "status": "error",
                "error": (proc.stderr or proc.stdout)[-2000:],
            }

        stdout = proc.stdout.strip()
        if not stdout:
            return {
                "status": "error",
                "error": "NotebookLM subprocess returned empty output",
            }
        try:
            return json.loads(stdout.splitlines()[-1])
        except json.JSONDecodeError as exc:
            return {
                "status": "error",
                "error": (
                    f"NotebookLM subprocess JSON error: {exc}; "
                    f"output={stdout[-1000:]}"
                ),
            }

    def verify_sources(self, *, force: bool = False) -> bool:
        with self._source_lock:
            if self._source_ids and not force:
                return True
            result = self._run_once("", None, sources_only=True)
            first_error = str(result.get("error") or result.get("hint") or "")
            if self._is_auth_error(first_error) and self._refresh_frontend_metadata():
                result = self._run_once("", None, sources_only=True)
            source_ids = result.get("_source_ids") or []
            if result.get("status") == "success" and source_ids:
                self._source_ids = list(source_ids)
                self._last_error = ""
                logger.info(
                    "NotebookLM cloud preflight OK: %s sources; timings=%s",
                    len(self._source_ids),
                    result.get("_timings", {}),
                )
                return True
            self._last_error = str(
                result.get("error") or result.get("hint") or "unknown error"
            )
            logger.error("NotebookLM cloud preflight failed: %s", self._last_error)
            return False

    def _ensure_sources(self) -> bool:
        if self._source_ids:
            return True
        return self.verify_sources()

    def query(self, query: str, chat_id: int = 0) -> str | None:
        with self._state_lock:
            chat_lock = self._chat_locks.setdefault(chat_id, threading.Lock())
        with chat_lock, self._query_slots:
            if not self._ensure_sources():
                return None
            for attempt in range(3):
                with self._state_lock:
                    conversation_id = (
                        self._conversations.get(chat_id)
                        if attempt == 0
                        else None
                    )
                result = self._run_once(
                    query,
                    conversation_id,
                    source_ids=list(self._source_ids),
                )
                answer = (result.get("answer") or "").strip()
                logger.info(
                    "NotebookLM cloud status=%s attempt=%s chars=%s timings=%s",
                    result.get("status"),
                    attempt + 1,
                    len(answer),
                    result.get("_timings", {}),
                )
                if result.get("status") == "success" and answer:
                    new_conversation = result.get("conversation_id")
                    if new_conversation:
                        with self._state_lock:
                            self._conversations[chat_id] = new_conversation
                    self._last_error = ""
                    return answer

                self._last_error = str(
                    result.get("error")
                    or result.get("hint")
                    or "NotebookLM returned an empty answer"
                )
                logger.warning(
                    "NotebookLM cloud attempt %s failed: %s",
                    attempt + 1,
                    self._last_error,
                )
                with self._state_lock:
                    self._conversations.pop(chat_id, None)
                if self._is_auth_error(self._last_error):
                    with self._source_lock:
                        self._source_ids = []
                    self._auth = self._load_auth()
                    self._persist_auth()
                    self._refresh_frontend_metadata()
                    self.verify_sources(force=True)
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))

            logger.error(
                "NotebookLM cloud query failed after 3 attempts: %s",
                self._last_error,
            )
            return None

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def source_count(self) -> int:
        return len(self._source_ids)

    def reset_conversation(self, chat_id: int) -> None:
        with self._state_lock:
            self._conversations.pop(chat_id, None)
