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
import subprocess
import sys
import threading
import time
from pathlib import Path


logger = logging.getLogger(__name__)


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

    @staticmethod
    def _load_auth() -> dict:
        encoded = os.getenv("NOTEBOOKLM_AUTH_JSON_B64", "").strip()
        plain = os.getenv("NOTEBOOKLM_AUTH_JSON", "").strip()

        try:
            if encoded:
                raw = base64.b64decode(encoded).decode("utf-8")
            elif plain:
                raw = plain
            else:
                raise NotebookConnectorError(
                    "Не задана облачная авторизация NotebookLM"
                )
            auth = json.loads(raw.lstrip("\ufeff"))
        except NotebookConnectorError:
            raise
        except Exception as exc:
            raise NotebookConnectorError(
                "Повреждена облачная авторизация NotebookLM"
            ) from exc

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
            "source_ids": list(source_ids or []),
            "sources_only": sources_only,
        }
        try:
            proc = subprocess.run(
                [sys.executable, "-c", script],
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=self.timeout,
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
                if "401" in self._last_error or "not authenticated" in self._last_error.lower():
                    with self._source_lock:
                        self._source_ids = []
                    self._auth = self._load_auth()
                    self._persist_auth()
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
