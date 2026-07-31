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


class EdgeTtsTests(unittest.TestCase):
    def test_chunk_is_sent_once_without_truncation(self):
        full_text = " ".join(
            f"Предложение номер {number} остаётся в полной озвучке."
            for number in range(150)
        )
        calls = []

        class FakeCommunicate:
            def __init__(self, text, **kwargs):
                calls.append((text, kwargs))

            async def save(self, path):
                with open(path, "wb") as target:
                    target.write(b"ID3" + b"audio" * 300)

        path = None
        try:
            with patch.object(main.edge_tts, "Communicate", FakeCommunicate):
                path = main._edge_chunk_to_speech(full_text)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], full_text)
            self.assertEqual(calls[0][1]["voice"], "ru-RU-DmitryNeural")
            self.assertEqual(calls[0][1]["rate"], "-5%")
            self.assertGreater(os.path.getsize(path), 1024)
        finally:
            if path and os.path.exists(path):
                os.unlink(path)

    def test_temporary_edge_failure_is_retried(self):
        attempts = 0

        class FlakyCommunicate:
            def __init__(self, _text, **_kwargs):
                pass

            async def save(self, path):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise RuntimeError("temporary network failure")
                with open(path, "wb") as target:
                    target.write(b"ID3" + b"audio" * 300)

        path = None
        try:
            with (
                patch.object(main.edge_tts, "Communicate", FlakyCommunicate),
                patch.object(main.time, "sleep"),
            ):
                path = main._edge_chunk_to_speech("Проверка повторных попыток")
            self.assertEqual(attempts, 3)
            self.assertTrue(os.path.exists(path))
        finally:
            if path and os.path.exists(path):
                os.unlink(path)

    def test_empty_text_is_rejected_without_network_call(self):
        with (
            patch.object(main.edge_tts, "Communicate") as communicate,
            self.assertRaises(ValueError),
        ):
            main._edge_chunk_to_speech("   ")
        communicate.assert_not_called()

    def test_split_preserves_full_text(self):
        full_text = " ".join(
            f"Предложение номер {number} остаётся в полной озвучке."
            for number in range(150)
        )
        parts = main._split_for_tts(full_text)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= main._EDGE_TTS_CHUNK_LIMIT for part in parts))
        self.assertEqual(" ".join(parts), full_text)

    def test_text_to_speech_returns_exactly_one_file(self):
        with (
            patch.object(main, "_split_for_tts", return_value=["Полный ответ"]),
            patch.object(
                main,
                "_edge_chunk_to_speech",
                return_value="voice.mp3",
            ) as synthesize,
        ):
            self.assertEqual(main._text_to_speech("Полный ответ"), ["voice.mp3"])
        synthesize.assert_called_once_with("Полный ответ")

    def test_parallel_parts_are_merged_into_one_file(self):
        with (
            patch.object(main, "_split_for_tts", return_value=["один", "два"]),
            patch.object(
                main,
                "_edge_chunk_to_speech",
                side_effect=lambda text: f"{text}.mp3",
            ) as synthesize,
            patch.object(
                main,
                "_merge_edge_audio",
                return_value="full.ogg",
            ) as merge,
        ):
            self.assertEqual(main._text_to_speech("полный ответ"), ["full.ogg"])
        self.assertEqual(synthesize.call_count, 2)
        merge.assert_called_once_with(["один.mp3", "два.mp3"])


class TtsUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_timeout_retries_same_file(self):
        fd, path = tempfile.mkstemp(suffix=".mp3")
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


class TtsButtonHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_answer_is_sent_as_one_voice_message(self):
        token = "singlevoice"
        main._tts_answers[token] = (main.ADMIN_ID, "Полный текст ответа.")
        main._tts_in_progress.clear()
        fd, ogg_path = tempfile.mkstemp(suffix=".mp3")
        os.write(fd, b"ID3")
        os.close(fd)
        message = SimpleNamespace(reply_text=AsyncMock())
        query = SimpleNamespace(
            data=f"tts:{token}",
            message=message,
            answer=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=main.ADMIN_ID),
            callback_query=query,
        )
        sender = AsyncMock()
        with (
            patch.object(main, "_text_to_speech", return_value=[ogg_path]),
            patch.object(main, "_send_voice_with_retry", sender),
        ):
            await main.handle_tts_button(update, SimpleNamespace())

        sender.assert_awaited_once_with(message, ogg_path, 1, 1)
        self.assertFalse(os.path.exists(ogg_path))
        self.assertIn(
            "одно голосовое сообщение",
            message.reply_text.await_args.args[0],
        )
        main._tts_answers.pop(token, None)


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
