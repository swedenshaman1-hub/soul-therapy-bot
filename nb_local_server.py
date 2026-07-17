"""
Локальный прокси-сервер для NotebookLM.
Запускается на компьютере пользователя (российский IP),
принимает запросы от Railway-бота.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

NOTEBOOK_ID = "88a124fc-a20d-4836-99a3-25b079468568"
SECRET = os.environ.get("NOTEBOOKLM_LOCAL_SECRET", "")

_conversations: dict[int, str] = {}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/ask":
            self.send_error(404)
            return

        # Проверка секрета
        if SECRET and self.headers.get("X-Secret") != SECRET:
            self.send_error(403)
            return

        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self.send_error(400)
            return

        query = body.get("query", "").strip()
        chat_id = int(body.get("chat_id", 0))

        if not query:
            self._json({"ok": False, "error": "empty query"})
            return

        try:
            from notebooklm_mcp_2026.tools.query import query_notebook
            conv_id = _conversations.get(chat_id)
            result = query_notebook(
                notebook_id=NOTEBOOK_ID,
                query=query,
                conversation_id=conv_id or None,
            )
            if result.get("status") == "success":
                new_conv = result.get("conversation_id")
                if new_conv:
                    _conversations[chat_id] = new_conv
                self._json({"ok": True, "answer": result.get("answer", "")})
            else:
                self._json({"ok": False, "error": result.get("error", "unknown")})
        except Exception as e:
            self._json({"ok": False, "error": str(e)})

    def _json(self, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(f"[NB-Server] {fmt % args}", flush=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    print(f"NotebookLM прокси запущен на порту {port}", flush=True)
    print(f"Секрет: {'задан' if SECRET else 'НЕ задан (открытый доступ)'}", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
