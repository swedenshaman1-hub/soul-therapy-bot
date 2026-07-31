import asyncio
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch


os.environ["SOUL_BOT_TOKEN"] = "test-token"
os.environ["GEMINI_API_KEY"] = "test-key"
os.environ["NOTEBOOKLM_AUTH_JSON"] = ""
os.environ["NOTEBOOKLM_AUTH_JSON_B64"] = ""
os.environ["NOTEBOOKLM_MCP_DATA_DIR"] = ""

import main


class TranscriptionTests(unittest.TestCase):
    def test_short_yes_and_no_are_valid(self):
        self.assertEqual(main._validate_transcript("Да."), "Да.")
        self.assertEqual(main._validate_transcript("Нет."), "Нет.")

    def test_empty_or_no_speech_is_rejected(self):
        for value in ("", None, "__NO_SPEECH__"):
            with self.subTest(value=value):
                with self.assertRaises(main.TranscriptionError):
                    main._validate_transcript(value)

    def test_instruction_leak_is_rejected(self):
        with self.assertRaises(main.TranscriptionError):
            main._validate_transcript(
                "Расшифруй это голосовое сообщение на русском языке."
            )

    def test_instruction_leak_is_retried_then_short_word_is_returned(self):
        responses = [
            SimpleNamespace(text="Расшифруй это голосовое сообщение на русском языке."),
            SimpleNamespace(text="Да."),
        ]
        generate = Mock(side_effect=responses)
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        fd, path = tempfile.mkstemp(suffix=".ogg")
        os.write(fd, b"fake audio")
        os.close(fd)
        try:
            with patch.object(main.google_genai, "Client", return_value=client):
                self.assertEqual(main._transcribe(path), "Да.")
        finally:
            os.unlink(path)

        self.assertEqual(generate.call_count, 2)
        first_call = generate.call_args_list[0]
        self.assertEqual(len(first_call.kwargs["contents"]), 1)
        config = first_call.kwargs["config"]
        self.assertEqual(config.system_instruction, main.TRANSCRIBE_PROMPT)
        self.assertEqual(config.temperature, 0)

    def test_two_instruction_leaks_fail_closed(self):
        response = SimpleNamespace(
            text="Ты выполняешь только распознавание русской речи из аудиозаписи."
        )
        generate = Mock(side_effect=[response, response])
        client = SimpleNamespace(models=SimpleNamespace(generate_content=generate))
        fd, path = tempfile.mkstemp(suffix=".ogg")
        os.write(fd, b"fake audio")
        os.close(fd)
        try:
            with patch.object(main.google_genai, "Client", return_value=client):
                with self.assertRaises(main.TranscriptionError):
                    main._transcribe(path)
        finally:
            os.unlink(path)


class TtsSplitTests(unittest.TestCase):
    def test_short_text_stays_whole(self):
        self.assertEqual(main._split_for_tts("Короткий текст."), ["Короткий текст."])

    def test_long_text_is_not_lost(self):
        text = " ".join(
            f"Предложение номер {number} содержит несколько слов."
            for number in range(100)
        )
        chunks = main._split_for_tts(text)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= main._TTS_CHUNK_LIMIT for chunk in chunks))
        self.assertEqual(" ".join(chunks), text)

    def test_long_sentence_is_not_cut_inside_word(self):
        text = ("длинноеслово " * 250).strip()
        chunks = main._split_for_tts(text)
        self.assertTrue(all(not chunk.endswith("длинноесл") for chunk in chunks))
        self.assertEqual(" ".join(chunks), text)


class TtsUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_timeout_retries_same_file(self):
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.write(fd, b"audio")
        os.close(fd)
        message = SimpleNamespace(
            reply_voice=AsyncMock(
                side_effect=[main.TimedOut("test timeout"), None]
            )
        )
        try:
            with patch.object(asyncio, "sleep", new=AsyncMock()):
                await main._send_voice_with_retry(message, path, 2, 4)
        finally:
            os.unlink(path)

        self.assertEqual(message.reply_voice.await_count, 2)
        for call in message.reply_voice.await_args_list:
            self.assertEqual(call.kwargs["caption"], "Часть 2 из 4")
            self.assertEqual(call.kwargs["write_timeout"], 120)


class VoiceHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_transcript_never_reaches_notebooklm(self):
        class FakeDownloadedFile:
            async def download_to_drive(self, path):
                with open(path, "wb") as target:
                    target.write(b"fake audio")

        message = SimpleNamespace(
            voice=SimpleNamespace(file_id="voice-id"),
            replies=[],
        )

        async def reply_text(text, **kwargs):
            message.replies.append((text, kwargs))

        message.reply_text = reply_text
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=main.ADMIN_ID),
            message=message,
        )
        context = SimpleNamespace(
            bot=SimpleNamespace(
                get_file=AsyncMock(return_value=FakeDownloadedFile())
            )
        )
        answer = AsyncMock()
        with (
            patch.object(
                main,
                "_transcribe",
                side_effect=main.TranscriptionError("instruction leak"),
            ),
            patch.object(main, "_answer", answer),
        ):
            await main.handle_voice(update, context)

        answer.assert_not_awaited()
        self.assertEqual(len(message.replies), 2)
        self.assertIn("Не удалось уверенно разобрать", message.replies[-1][0])


if __name__ == "__main__":
    unittest.main()
