"""
Телеграм-бот: Коуч по методу Терапии Души (Евгений Теребенин)
- Принимает вопросы текстом и голосом
- Отвечает в стиле живого коуча, опираясь на 299 источников метода
- NotebookLM — база знаний, Gemini — постобработка в коучинговый стиль
"""

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave
from collections import defaultdict
from functools import partial

from dotenv import load_dotenv
from google import genai as google_genai
from google.genai import types as genai_types
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# На Railway: восстанавливаем auth.json из переменной окружения
_nb_auth_json = os.getenv("NOTEBOOKLM_AUTH_JSON", "").strip()
_nb_data_dir = os.getenv("NOTEBOOKLM_MCP_DATA_DIR", "").strip()
if _nb_auth_json and _nb_data_dir:
    import httpx as _httpx
    os.makedirs(_nb_data_dir, exist_ok=True)
    _auth_path = os.path.join(_nb_data_dir, "auth.json")
    _auth_data = json.loads(_nb_auth_json)
    # Keep refreshed credentials from a persistent Railway volume. The env var
    # is only a bootstrap copy and may be older after a restart or deployment.
    if os.path.exists(_auth_path):
        try:
            with open(_auth_path, encoding="utf-8") as _f:
                _disk_auth = json.load(_f)
            if float(_disk_auth.get("extracted_at", 0) or 0) >= float(_auth_data.get("extracted_at", 0) or 0):
                _auth_data = _disk_auth
                print("Startup auth: using newer persistent auth.json", flush=True)
        except Exception as _e:
            print(f"Startup auth: persistent auth.json ignored: {_e}", flush=True)
    # Пробуем получить свежий CSRF с NotebookLM до запуска клиента.
    # GenerateFreeFormStreamed строго валидирует CSRF, а batchexecute — нет.
    try:
        _jar = _httpx.Cookies()
        for _k, _v in _auth_data.get("cookies", {}).items():
            _jar.set(_k, _v, domain=".google.com")
        _hdrs = {
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        }
        with _httpx.Client(cookies=_jar, headers=_hdrs, follow_redirects=True, timeout=20.0) as _hc:
            _pg = _hc.get("https://notebooklm.google.com/")
        if _pg.status_code == 200 and "accounts.google.com" not in str(_pg.url):
            _m = re.search(r'"SNlM0e":"([^"]+)"', _pg.text)
            if _m:
                _auth_data["csrf_token"] = _m.group(1)
                _m2 = re.search(r'"FdrFJe":"(\d+)"', _pg.text)
                if _m2:
                    _auth_data["session_id"] = _m2.group(1)
                print(f"Startup CSRF OK: {_auth_data['csrf_token'][:35]}...", flush=True)
            else:
                print("Startup CSRF: SNlM0e not in page, using stored token", flush=True)
            # Авто-определяем build label — Google меняет его раз в несколько недель.
            # Устанавливаем env var ДО первого импорта notebooklm пакета,
            # поэтому пакет подхватит актуальное значение автоматически.
            _bl = re.search(r'boq_labs-tailwind-frontend_[\w.]+', _pg.text)
            if _bl:
                _detected_bl = _bl.group(0).rstrip('.')
                os.environ["NOTEBOOKLM_BL"] = _detected_bl
                print(f"Build label auto-detected: {_detected_bl}", flush=True)
        else:
            print(f"Startup CSRF: page {_pg.status_code}, using stored token", flush=True)
    except Exception as _e:
        print(f"Startup CSRF refresh failed, using stored token: {_e}", flush=True)
        # Retry up to 2 more times with delay
        for _retry in range(2):
            try:
                import time as _t
                _t.sleep(10 * (_retry + 1))
                print(f"Startup CSRF retry {_retry + 1}...", flush=True)
                with _httpx.Client(cookies=_jar, headers=_hdrs, follow_redirects=True, timeout=20.0) as _hc:
                    _pg = _hc.get("https://notebooklm.google.com/")
                if _pg.status_code == 200 and "accounts.google.com" not in str(_pg.url):
                    _m = re.search(r'"SNlM0e":"([^"]+)"', _pg.text)
                    if _m:
                        _auth_data["csrf_token"] = _m.group(1)
                        _m2 = re.search(r'"FdrFJe":"(\d+)"', _pg.text)
                        if _m2:
                            _auth_data["session_id"] = _m2.group(1)
                        print(f"Startup CSRF OK (retry {_retry + 1}): {_auth_data['csrf_token'][:35]}...", flush=True)
                    _bl = re.search(r'boq_labs-tailwind-frontend_[\w.]+', _pg.text)
                    if _bl:
                        _detected_bl = _bl.group(0).rstrip('.')
                        os.environ["NOTEBOOKLM_BL"] = _detected_bl
                        print(f"Build label auto-detected (retry {_retry + 1}): {_detected_bl}", flush=True)
                    break
            except Exception as _re:
                print(f"Startup CSRF retry {_retry + 1} failed: {_re}", flush=True)
    with open(_auth_path, "w", encoding="utf-8") as _f:
        json.dump(_auth_data, _f)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.getenv("SOUL_BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()

# Ограничение доступа — только владелец. Узнать свой chat_id: написать боту /id
ALLOWED_CHAT_IDS: set[int] = set()  # пусто = открыт для всех; добавь: {123456789}

NOTEBOOK_ID = "88a124fc-a20d-4836-99a3-25b079468568"
# На Windows используем uv-окружение, на Linux (Railway) — системный Python
_WIN_MCP_PYTHON = r"C:\Users\Admin\AppData\Roaming\uv\tools\notebooklm-mcp-2026\Scripts\python.exe"
MCP_PYTHON = _WIN_MCP_PYTHON if os.path.exists(_WIN_MCP_PYTHON) else sys.executable

# История диалога: chat_id -> список {"role": "user"|"assistant", "text": str}
_history: dict[int, list[dict]] = defaultdict(list)
HISTORY_LIMIT = 6  # последних реплик (3 обмена)

# ─── Промпты ──────────────────────────────────────────────────────────────────

TRANSCRIBE_PROMPT = """Расшифруй это голосовое сообщение на русском языке.

Контекст: пользователь задаёт вопросы об авторском методе «Терапия Души» психолога Евгения Теребенина.
Термины: Терапия Души, слайды, родовые программы, Дух, Душа, Тело, кинезиологический тест, Триморф, Собор, 7-шаговый алгоритм.

Правила:
- Пиши точно как сказано, без пересказа
- Только текст расшифровки, без комментариев"""


def _build_notebooklm_query(question: str, history: list[dict]) -> str:
    """Формирует запрос в NotebookLM с контекстом беседы."""
    context = ""
    if history:
        lines = []
        for msg in history[-4:]:
            role = "Ученик" if msg["role"] == "user" else "Коуч"
            lines.append(f"{role}: {msg['text']}")
        context = "Контекст предыдущего диалога:\n" + "\n".join(lines) + "\n\n"

    return (
        f"{context}"
        f"Вопрос ученика по методу Терапии Души Евгения Теребенина:\n{question}\n\n"
        "Дай развёрнутый ответ, опираясь на материалы метода."
    )


def _strip_markdown(text: str) -> str:
    """Убирает markdown-форматирование и цитатные индексы из текста."""
    # Сноски вида [1], [1, 2], [1-3]
    text = re.sub(r'\s*\[\d+(?:[,\-\s]\s*\d+)*\]', '', text)
    # Заголовки ### ## #
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Жирный и курсив **text**, *text*, __text__, _text_
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'_(.+?)_', r'\1', text)
    # Маркеры списков в начале строки: *, -, •, 1.
    text = re.sub(r'^\s*[\*\-•]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    # Лишние пустые строки
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _refresh_notebooklm_auth_sync() -> bool:
    """Обновляет CSRF и build label NotebookLM без перезапуска бота."""
    nb_auth_json = os.getenv("NOTEBOOKLM_AUTH_JSON", "").strip()
    nb_data_dir = os.getenv("NOTEBOOKLM_MCP_DATA_DIR", "").strip()
    if not nb_auth_json or not nb_data_dir:
        return False

    import httpx as _h
    auth_data = json.loads(nb_auth_json)
    auth_path = os.path.join(nb_data_dir, "auth.json")
    if os.path.exists(auth_path):
        try:
            with open(auth_path, encoding="utf-8") as f:
                disk_auth = json.load(f)
            if float(disk_auth.get("extracted_at", 0) or 0) >= float(auth_data.get("extracted_at", 0) or 0):
                auth_data = disk_auth
        except Exception as e:
            logger.warning(f"NB refresh: persistent auth ignored: {e}")
    jar = _h.Cookies()
    for k, v in auth_data.get("cookies", {}).items():
        jar.set(k, v, domain=".google.com")
    hdrs = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        with _h.Client(cookies=jar, headers=hdrs, follow_redirects=True, timeout=25.0) as hc:
            pg = hc.get("https://notebooklm.google.com/")
    except Exception as e:
        logger.warning(f"NB periodic refresh: page fetch failed: {e}")
        return False

    if pg.status_code != 200 or "accounts.google.com" in str(pg.url):
        logger.warning(f"NB periodic refresh: unexpected page {pg.status_code} url={pg.url}")
        return False

    new_csrf = None
    m = re.search(r'"SNlM0e":"([^"]+)"', pg.text)
    if m:
        new_csrf = m.group(1)
        auth_data["csrf_token"] = new_csrf
        m2 = re.search(r'"FdrFJe":"(\d+)"', pg.text)
        if m2:
            auth_data["session_id"] = m2.group(1)
        try:
            with open(os.path.join(nb_data_dir, "auth.json"), "w", encoding="utf-8") as f:
                json.dump(auth_data, f)
        except Exception as e:
            logger.warning(f"NB periodic refresh: couldn't write auth.json: {e}")

    bl_m = re.search(r'boq_labs-tailwind-frontend_[\w.]+', pg.text)
    new_bl = bl_m.group(0).rstrip('.') if bl_m else None
    if new_bl:
        os.environ["NOTEBOOKLM_BL"] = new_bl

    # Patch running notebooklm modules if already imported
    import sys as _sys
    nb_cfg = _sys.modules.get("notebooklm_mcp_2026.config")
    nb_srv = _sys.modules.get("notebooklm_mcp_2026.server")
    if nb_cfg and new_bl:
        bl_changed = nb_cfg.BUILD_LABEL != new_bl
        nb_cfg.BUILD_LABEL = new_bl
        if nb_srv:
            if bl_changed:
                logger.info(f"NB periodic refresh: BL changed → {new_bl}, resetting client")
                nb_srv.reset_client()
            elif new_csrf and nb_srv._client:
                nb_srv._client.csrf_token = new_csrf
                logger.info(f"NB periodic refresh: CSRF patched on client {new_csrf[:30]}...")

    logger.info(f"NB periodic refresh OK: BL={new_bl or 'N/A'} CSRF={'OK' if new_csrf else 'N/A'}")
    return True


async def _periodic_nb_refresh_job(context: ContextTypes.DEFAULT_TYPE):
    """Job: каждые 3 часа обновляем CSRF и build label."""
    def _locked_refresh():
        global _nb_last_refresh_at
        with _nb_query_lock:
            ok = _refresh_notebooklm_auth_sync()
            if ok:
                _nb_last_refresh_at = time.time()
            return ok

    await _run_blocking(_locked_refresh)


COACH_SYSTEM_PROMPT = """Ты — коуч и наставник, глубоко знающий метод Терапия Души психолога и тренера Евгения Валентиновича Теребенина.

Твоя роль: обучать методу Терапии Души на основе авторских материалов Теребенина. Отвечать тепло, живо и поддерживающе — как опытный наставник в живом разговоре, а не как энциклопедия. Использовать термины метода естественно: слайды, родовые программы, Триморф, Собор, кинезиологический тест, 7-шаговый алгоритм, Дух, Душа, Тело. Давать практические примеры и пояснения. При необходимости задавать уточняющие вопросы.

Когда упоминаешь автора метода, используй только «Евгений Валентинович» или «Евгений Валентинович Теребенин». Никогда не пиши «Женя», «Женечка» или другие уменьшительные формы.

Формат ответа — ОБЯЗАТЕЛЬНО:
Пиши сплошным живым текстом, как говоришь вслух. Никаких звёздочек, никаких дефисов в начале строк, никаких тире как маркеров списка, никаких кавычек-ёлочек, никаких заголовков с решётками, никакого markdown вообще. Только обычные слова и предложения. Абзацы разделяй пустой строкой. Длина ответа — не более 400 слов. Завершай ответ коротким вопросом или приглашением к следующему шагу."""


def _coach_reformat(raw_answer: str, question: str, history: list[dict]) -> str:
    """Переформатирует ответ NotebookLM в живой коучинговый стиль через Gemini."""
    client = google_genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=genai_types.HttpOptions(timeout=60_000),
    )

    history_text = ""
    if history:
        lines = []
        for msg in history[-4:]:
            role = "Ученик" if msg["role"] == "user" else "Коуч"
            lines.append(f"{role}: {msg['text']}")
        history_text = "\n\nКонтекст диалога:\n" + "\n".join(lines)

    prompt = (
        f"{COACH_SYSTEM_PROMPT}\n\n"
        f"Вопрос ученика: {question}{history_text}\n\n"
        f"Информация из материалов метода (используй как источник, перепиши своими словами):\n{raw_answer}\n\n"
        "Дай ответ в роли коуча. Только ответ, без вводных фраз типа 'Конечно!' или 'Отличный вопрос!'."
    )

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
    )
    return response.text.strip()


# ─── NotebookLM через MCP ─────────────────────────────────────────────────────

# conversation_id для продолжения диалога в NotebookLM (по chat_id)
_nb_conversations: dict[int, str] = {}
_nb_query_lock = threading.Lock()
_nb_last_refresh_at = 0.0
_nb_last_error = ""
_NB_REFRESH_MAX_AGE = 20 * 60


_NB_LOCAL_URL = os.getenv("NOTEBOOKLM_LOCAL_URL", "").strip().rstrip("/")
_NB_LOCAL_SECRET = os.getenv("NOTEBOOKLM_LOCAL_SECRET", "").strip()


def _ask_notebooklm(query: str, chat_id: int = 0) -> str | None:
    """Запрашивает NotebookLM — через локальный прокси или прямой импорт."""
    logger.info(f"NotebookLM query: {query[:80]}")

    if _NB_LOCAL_URL:
        # Режим прокси: запрос на локальный сервер пользователя (российский IP)
        try:
            import urllib.request
            payload = json.dumps({"query": query, "chat_id": chat_id}).encode("utf-8")
            req = urllib.request.Request(
                f"{_NB_LOCAL_URL}/ask",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Secret": _NB_LOCAL_SECRET,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                answer = data.get("answer", "").strip()
                logger.info(f"NotebookLM proxy: получен ответ {len(answer)} симв.")
                return answer or None
            else:
                logger.error(f"NotebookLM proxy error: {data.get('error')}")
                return None
        except Exception as e:
            logger.exception(f"NotebookLM proxy exception: {e}")
            return None

    # Fallback: прямой импорт
    conv_id = _nb_conversations.get(chat_id)
    from notebooklm_mcp_2026.tools.query import query_notebook
    from notebooklm_mcp_2026 import server as _nb_server

    for _attempt in range(2):
        try:
            result = query_notebook(
                notebook_id=NOTEBOOK_ID,
                query=query,
                conversation_id=conv_id or None,
            )
            logger.info(f"NotebookLM result status: {result.get('status')} | attempt={_attempt}")
            if result.get("status") == "success":
                new_conv = result.get("conversation_id")
                if new_conv:
                    _nb_conversations[chat_id] = new_conv
                return result.get("answer", "").strip() or None

            error = result.get("error", "")
            # При 401 обновляем CSRF и повторяем
            if "401" in str(error) and _attempt == 0:
                logger.info("NotebookLM 401, refreshing CSRF and retrying...")
                try:
                    _client = _nb_server.get_client()
                    _client._refresh_auth_tokens()
                except Exception as _re:
                    logger.warning(f"CSRF refresh failed: {_re}")
                    _nb_server.reset_client()
                continue

            logger.error(f"NotebookLM error: {error} | hint: {result.get('hint','')}")
            return None
        except Exception as e:
            logger.exception(f"NotebookLM exception: {e}")
            return None
    return None


# ─── Транскрипция голоса ──────────────────────────────────────────────────────

def _ask_notebooklm_resilient(query: str, chat_id: int = 0) -> str | None:
    """Query NotebookLM with proactive auth refresh and bounded recovery."""
    global _nb_last_refresh_at, _nb_last_error

    # Keep the existing optional proxy mode intact.
    if _NB_LOCAL_URL:
        return _ask_notebooklm_direct(query, chat_id)

    with _nb_query_lock:
        from notebooklm_mcp_2026.tools.query import query_notebook
        from notebooklm_mcp_2026 import server as _nb_server

        if time.time() - _nb_last_refresh_at > _NB_REFRESH_MAX_AGE:
            if _refresh_notebooklm_auth_sync():
                _nb_last_refresh_at = time.time()
                _nb_server.reset_client()

        for attempt in range(3):
            try:
                conversation_id = _nb_conversations.get(chat_id) if attempt == 0 else None
                result = query_notebook(
                    notebook_id=NOTEBOOK_ID,
                    query=query,
                    conversation_id=conversation_id,
                )
                answer = (result.get("answer") or "").strip()
                logger.info(
                    "NotebookLM result status=%s attempt=%s answer_chars=%s",
                    result.get("status"), attempt + 1, len(answer),
                )
                if result.get("status") == "success" and answer:
                    new_conversation = result.get("conversation_id")
                    if new_conversation:
                        _nb_conversations[chat_id] = new_conversation
                    _nb_last_error = ""
                    return answer

                _nb_last_error = str(
                    result.get("error") or result.get("hint") or "empty response"
                )
                logger.warning(
                    "NotebookLM attempt %s failed: %s", attempt + 1, _nb_last_error
                )
            except Exception as exc:
                _nb_last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("NotebookLM attempt %s exception", attempt + 1)

            if attempt < 2:
                # Retry from a clean conversation and a fresh singleton client.
                _nb_conversations.pop(chat_id, None)
                _nb_server.reset_client()
                if _refresh_notebooklm_auth_sync():
                    _nb_last_refresh_at = time.time()
                time.sleep(1.5 * (attempt + 1))

        logger.error("NotebookLM failed after 3 attempts: %s", _nb_last_error)
        return None


# All handlers use the resilient implementation. Keeping the original function
# above preserves the optional local proxy path without duplicating that code.
_ask_notebooklm_direct = _ask_notebooklm
_ask_notebooklm = _ask_notebooklm_resilient


def _transcribe(file_path: str) -> str:
    with open(file_path, "rb") as f:
        audio_bytes = f.read()
    client = google_genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=genai_types.HttpOptions(timeout=120_000),
    )
    for attempt in range(5):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    genai_types.Part.from_bytes(data=audio_bytes, mime_type="audio/ogg"),
                    TRANSCRIBE_PROMPT,
                ],
            )
            return response.text.strip()
        except Exception as e:
            if ("503" in str(e) or "UNAVAILABLE" in str(e)) and attempt < 4:
                time.sleep(5 * (attempt + 1))
                continue
            raise


# ─── TTS через Gemini ─────────────────────────────────────────────────────────

_TTS_CHUNK_LIMIT = 4000  # символов на один TTS-запрос


def _tts_chunk(text: str) -> str:
    """Генерирует один WAV-файл из текста (до _TTS_CHUNK_LIMIT символов)."""
    client = google_genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=genai_types.HttpOptions(timeout=300_000),
    )
    for attempt in range(4):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash-preview-tts",
                contents=text,
                config=genai_types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    speech_config=genai_types.SpeechConfig(
                        voice_config=genai_types.VoiceConfig(
                            prebuilt_voice_config=genai_types.PrebuiltVoiceConfig(
                                voice_name="Sadaltager"
                            )
                        )
                    ),
                ),
            )
            break
        except Exception as e:
            err_lower = str(e).lower()
            transient = any(x in err_lower for x in (
                "deadline_exceeded", "504", "503", "timeout", "timed out",
                "unavailable", "resource_exhausted", "429",
            ))
            if transient and attempt < 3:
                time.sleep(15 * (attempt + 1))
                continue
            raise

    pcm_data = response.candidates[0].content.parts[0].inline_data.data
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm_data)
    return path


def _split_for_tts(text: str) -> list[str]:
    """Делит текст на части по _TTS_CHUNK_LIMIT символов, разбивая по предложениям."""
    if len(text) <= _TTS_CHUNK_LIMIT:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > _TTS_CHUNK_LIMIT:
        cut = remaining[:_TTS_CHUNK_LIMIT]
        # Режем по последней точке чтобы не обрывать предложение на полуслове
        last_dot = cut.rfind(".")
        if last_dot > _TTS_CHUNK_LIMIT // 2:
            cut = cut[:last_dot + 1]
        chunks.append(cut.strip())
        remaining = remaining[len(cut):].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _text_to_speech(text: str) -> list[str]:
    """Возвращает список путей к WAV-файлам (один или несколько если текст длинный)."""
    parts = _split_for_tts(text)
    paths: list[str] = []
    for part in parts:
        paths.append(_tts_chunk(part))
    return paths


# ─── Вспомогательные ─────────────────────────────────────────────────────────

async def _run_blocking(func, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, partial(func, *args))


async def _send_long(update: Update, text: str):
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i:i + 4000])


def _is_allowed(chat_id: int) -> bool:
    return not ALLOWED_CHAT_IDS or chat_id in ALLOWED_CHAT_IDS


async def _answer(update: Update, question: str):
    chat_id = update.effective_chat.id
    history = _history[chat_id]

    # Шаг 1: запрос в NotebookLM
    await update.message.reply_text("Ищу в материалах метода... ⏳")
    query = _build_notebooklm_query(question, history)
    raw = await _run_blocking(_ask_notebooklm, query, chat_id)

    if not raw:
        await update.message.reply_text(
            "Не удалось получить ответ из базы знаний. "
            "Попробуй переформулировать вопрос или повторить чуть позже."
        )
        return

    # Шаг 2: переформатирование через Gemini
    await update.message.reply_text("Формулирую ответ... 💭")
    try:
        answer = await _run_blocking(_coach_reformat, raw, question, history)
    except Exception as e:
        logger.exception("Gemini reformat error")
        answer = raw  # fallback — отдаём сырой ответ
    answer = _strip_markdown(answer)

    # Сохраняем в историю
    history.append({"role": "user", "text": question})
    history.append({"role": "assistant", "text": answer[:500]})  # сокращаем чтоб не разбухало
    if len(history) > HISTORY_LIMIT:
        _history[chat_id] = history[-HISTORY_LIMIT:]

    # Текстовый ответ
    await _send_long(update, answer)

    # Голосовой ответ
    audio_paths: list[str] = []
    try:
        await update.message.reply_text("Озвучиваю... 🎙")
        audio_paths = await _run_blocking(_text_to_speech, answer)
        for path in audio_paths:
            with open(path, "rb") as f:
                await update.message.reply_voice(f)
    except Exception as e:
        logger.exception("TTS error")
        await update.message.reply_text(f"Голос не удалось сгенерировать: {e}")
    finally:
        for path in audio_paths:
            try:
                os.unlink(path)
            except Exception:
                pass


# ─── Обработчики Telegram ────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    _history[chat_id].clear()
    await update.message.reply_text(
        "Привет! Я коуч по методу Терапии Души Евгения Теребенина.\n\n"
        "Задавай вопросы текстом или голосом — отвечу по авторским материалам метода.\n\n"
        "С чего хочешь начать?\n"
        "— Основы метода и его философия\n"
        "— Что такое слайды и как с ними работать\n"
        "— 7-шаговый алгоритм сессии\n"
        "— Родовые программы и как их освобождать\n\n"
        "/reset — начать диалог заново\n"
        "/id — узнать свой Telegram ID"
    )


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    _history[chat_id].clear()
    _nb_conversations.pop(chat_id, None)
    await update.message.reply_text("Диалог сброшен. Начинаем с чистого листа. О чём поговорим?")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твой Telegram chat_id: `{update.effective_chat.id}`",
                                     parse_mode="Markdown")


async def cmd_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = []

    # 1. Env vars
    auth_json_set = bool(os.getenv("NOTEBOOKLM_AUTH_JSON", "").strip())
    data_dir = os.getenv("NOTEBOOKLM_MCP_DATA_DIR", "").strip()
    lines.append(f"NOTEBOOKLM_AUTH_JSON задан: {auth_json_set}")
    lines.append(f"NOTEBOOKLM_MCP_DATA_DIR: {data_dir or '(не задан)'}")

    # 2. auth.json на диске
    if data_dir:
        auth_path = os.path.join(data_dir, "auth.json")
        exists = os.path.exists(auth_path)
        lines.append(f"auth.json существует: {exists}")
        if exists:
            try:
                with open(auth_path) as f:
                    data = json.load(f)
                cookies = data.get("cookies", {})
                lines.append(f"Кук в файле: {list(cookies.keys())[:4]}...")
            except Exception as e:
                lines.append(f"Ошибка чтения auth.json: {e}")
    else:
        lines.append("auth.json: путь не задан")

    # 3. Тест NotebookLM
    lines.append("\nЗапрашиваю NotebookLM (тест)...")
    await update.message.reply_text("\n".join(lines))
    lines = []

    try:
        from notebooklm_mcp_2026.tools.query import query_notebook
        result = query_notebook(notebook_id=NOTEBOOK_ID, query="Что такое слайды?")
        status = result.get("status")
        error = result.get("error", "")
        hint = result.get("hint", "")
        answer = result.get("answer", "")
        lines.append(f"Статус: {status}")
        if error:
            lines.append(f"Ошибка: {error}")
        if hint:
            lines.append(f"Подсказка: {hint}")
        if answer:
            lines.append(f"Ответ (первые 200 симв.):\n{answer[:200]}")
    except Exception as e:
        import traceback
        lines.append(f"Исключение: {e}")
        lines.append(traceback.format_exc()[-800:])

    await update.message.reply_text("\n".join(lines))


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_chat.id):
        return
    question = (update.message.text or "").strip()
    if not question:
        return
    await _answer(update, question)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_chat.id):
        return

    await update.message.reply_text("Расшифровываю... 🎤")
    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = tmp.name
    try:
        question = await _run_blocking(_transcribe, tmp_path)
        await update.message.reply_text(f"_{question}_", parse_mode="Markdown")
        await _answer(update, question)
    except Exception as e:
        logger.exception("Transcription error")
        await update.message.reply_text(f"Не удалось расшифровать: {e}")
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# ─── Запуск ──────────────────────────────────────────────────────────────────

def main():
    if not TELEGRAM_TOKEN:
        print("SOUL_BOT_TOKEN не задан в .env")
        sys.exit(1)
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY не задан в .env")
        sys.exit(1)

    print("Коуч Терапии Души запускается...")
    if ALLOWED_CHAT_IDS:
        print(f"Доступ ограничен: {ALLOWED_CHAT_IDS}")
    else:
        print("Доступ открыт для всех (задай ALLOWED_CHAT_IDS для ограничения)")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("debug", cmd_debug))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Периодически обновляем CSRF и build label (каждые 3 часа, первый запуск через 5 мин)
    if app.job_queue:
        app.job_queue.run_repeating(_periodic_nb_refresh_job, interval=1800, first=10)
        print("Periodic NotebookLM auth refresh scheduled (every 30m)", flush=True)

    print("Бот запущен. Ожидаю сообщения...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
