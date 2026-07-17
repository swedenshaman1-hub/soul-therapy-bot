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
import subprocess
import sys
import tempfile
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
    os.makedirs(_nb_data_dir, exist_ok=True)
    _auth_path = os.path.join(_nb_data_dir, "auth.json")
    if not os.path.exists(_auth_path):
        with open(_auth_path, "w", encoding="utf-8") as _f:
            _f.write(_nb_auth_json)

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
MCP_PYTHON = r"C:\Users\Admin\AppData\Roaming\uv\tools\notebooklm-mcp-2026\Scripts\python.exe"

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


COACH_SYSTEM_PROMPT = """Ты — коуч и наставник, глубоко знающий метод Терапия Души психолога и тренера Евгения Теребенина.

Твоя роль: обучать методу Терапии Души на основе авторских материалов Теребенина. Отвечать тепло, живо и поддерживающе — как опытный наставник в живом разговоре, а не как энциклопедия. Использовать термины метода естественно: слайды, родовые программы, Триморф, Собор, кинезиологический тест, 7-шаговый алгоритм, Дух, Душа, Тело. Давать практические примеры и пояснения. При необходимости задавать уточняющие вопросы.

Формат ответа — ОБЯЗАТЕЛЬНО:
Пиши сплошным живым текстом, как говоришь вслух. Никаких звёздочек, никаких дефисов в начале строк, никаких тире как маркеров списка, никаких кавычек-ёлочек, никаких заголовков с решётками, никакого markdown вообще. Только обычные слова и предложения. Абзацы разделяй пустой строкой. Завершай ответ коротким вопросом или приглашением к следующему шагу."""


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


def _ask_notebooklm(query: str, chat_id: int = 0) -> str | None:
    """Запрашивает NotebookLM через notebooklm-mcp-2026."""
    conv_id = _nb_conversations.get(chat_id)

    script = (
        "import sys, json\n"
        "sys.stdout.reconfigure(encoding='utf-8')\n"
        "from notebooklm_mcp_2026.tools.query import query_notebook\n"
        f"r = query_notebook({NOTEBOOK_ID!r}, {query!r}"
        + (f", conversation_id={conv_id!r}" if conv_id else "")
        + ")\n"
        "print(json.dumps(r, ensure_ascii=False))\n"
    )

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    try:
        result = subprocess.run(
            [MCP_PYTHON, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            env=env,
        )
        if result.returncode != 0:
            logger.error(f"NotebookLM error: {result.stderr[:500]}")
            return None
        data = json.loads(result.stdout.strip())
        if data.get("status") == "success":
            new_conv = data.get("conversation_id")
            if new_conv:
                _nb_conversations[chat_id] = new_conv
            return data.get("answer", "").strip() or None
        else:
            logger.error(f"NotebookLM returned error: {data.get('error')}")
            return None
    except subprocess.TimeoutExpired:
        logger.error("NotebookLM timeout")
    except Exception as e:
        logger.exception(f"NotebookLM error: {e}")
    return None


# ─── Транскрипция голоса ──────────────────────────────────────────────────────

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

def _text_to_speech(text: str) -> str:
    # Берём до 2500 символов, обрезаем по границе предложения
    tts_text = text[:2500].rsplit(".", 1)[0] + "." if len(text) > 2500 else text
    client = google_genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=genai_types.HttpOptions(timeout=300_000),
    )
    for attempt in range(3):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash-preview-tts",
                contents=tts_text,
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
            if any(x in str(e) for x in ("DEADLINE_EXCEEDED", "504", "timeout")) and attempt < 2:
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

    # Сохраняем в историю
    history.append({"role": "user", "text": question})
    history.append({"role": "assistant", "text": answer[:500]})  # сокращаем чтоб не разбухало
    if len(history) > HISTORY_LIMIT:
        _history[chat_id] = history[-HISTORY_LIMIT:]

    # Текстовый ответ
    await _send_long(update, answer)

    # Голосовой ответ
    audio_path = None
    try:
        await update.message.reply_text("Озвучиваю... 🎙")
        audio_path = await _run_blocking(_text_to_speech, answer)
        with open(audio_path, "rb") as f:
            await update.message.reply_voice(f)
    except Exception as e:
        logger.exception("TTS error")
        await update.message.reply_text(f"Голос не удалось сгенерировать: {e}")
    finally:
        if audio_path:
            try:
                os.unlink(audio_path)
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
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Бот запущен. Ожидаю сообщения...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
