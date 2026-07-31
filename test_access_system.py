import os
import re
import shutil
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


TEST_DIR = tempfile.mkdtemp(prefix="soul-bot-access-")
os.environ["ACCESS_DB_PATH"] = os.path.join(TEST_DIR, "access.db")
os.environ["NOTEBOOKLM_AUTH_JSON"] = ""
os.environ["NOTEBOOKLM_AUTH_JSON_B64"] = ""
os.environ["NOTEBOOKLM_MCP_DATA_DIR"] = ""
os.environ["SOUL_BOT_TOKEN"] = "test-token"
os.environ["GEMINI_API_KEY"] = "test-key"

import access_control as access_db
import main


class FakeMessage:
    def __init__(self, chat_id=100, on_reply=None):
        self.chat_id = chat_id
        self.replies = []
        self.voices = []
        self.on_reply = on_reply

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        if self.on_reply:
            self.on_reply(len(self.replies))
        return SimpleNamespace(message_id=len(self.replies))

    async def reply_voice(self, voice, **kwargs):
        self.voices.append((voice, kwargs))


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))


def make_update(user_id, chat_id=None, text="вопрос", callback_data=None):
    chat_id = chat_id if chat_id is not None else user_id
    message = FakeMessage(chat_id=chat_id)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(
            id=user_id,
            full_name=f"User {user_id}",
            username=f"user{user_id}",
        ),
        effective_chat=SimpleNamespace(id=chat_id),
        message=message,
        callback_query=None,
    )
    message.text = text
    if callback_data is not None:
        update.callback_query = FakeQuery(callback_data, message)
    return update


class AccessStorageTests(unittest.TestCase):
    def setUp(self):
        if os.path.exists(access_db.DB_PATH):
            os.unlink(access_db.DB_PATH)
        access_db.init_db()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TEST_DIR, ignore_errors=True)

    def test_admin_always_has_access(self):
        self.assertTrue(main._is_admin(1288155468))
        self.assertTrue(main._is_allowed(1288155468))
        self.assertFalse(main._is_admin(1288155469))

    def test_one_time_activation_and_no_extension_on_repeat(self):
        token = access_db.create_invite(main.ADMIN_ID, 7)
        status, expires_at = access_db.activate_invite(
            token, 101, "Первый", "first"
        )
        self.assertEqual(status, "activated")
        self.assertTrue(access_db.has_active_access(101))

        repeat_status, repeat_expiry = access_db.activate_invite(
            token, 101, "Первый", "first"
        )
        self.assertEqual(repeat_status, "already")
        self.assertEqual(repeat_expiry, expires_at)

        second_status, _ = access_db.activate_invite(
            token, 202, "Второй", "second"
        )
        self.assertEqual(second_status, "used")
        self.assertFalse(access_db.has_active_access(202))

    def test_expired_invite_cannot_be_reactivated(self):
        token = access_db.create_invite(main.ADMIN_ID, 7)
        access_db.activate_invite(token, 303, "Истёк", None)
        with access_db._db() as connection:
            connection.execute(
                "UPDATE invites SET expires_at = 1 WHERE token = ?", (token,)
            )
            connection.execute(
                "UPDATE access_users SET expires_at = 1 WHERE chat_id = 303"
            )

        status, expires_at = access_db.activate_invite(
            token, 303, "Истёк", None
        )
        self.assertEqual(status, "already")
        self.assertEqual(expires_at, 1)
        self.assertFalse(access_db.has_active_access(303))

    def test_concurrent_activation_has_single_winner(self):
        token = access_db.create_invite(main.ADMIN_ID, 7)
        barrier = threading.Barrier(2)

        def activate(user_id):
            barrier.wait()
            return access_db.activate_invite(
                token, user_id, f"User {user_id}", None
            )[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(activate, (401, 402)))

        self.assertEqual(statuses.count("activated"), 1)
        self.assertEqual(statuses.count("used"), 1)

    def test_database_context_closes_connection(self):
        connection = access_db._connect()
        with patch.object(access_db, "_connect", return_value=connection):
            access_db.init_db()
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


class HandlerSecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        if os.path.exists(access_db.DB_PATH):
            os.unlink(access_db.DB_PATH)
        access_db.init_db()
        main._history.clear()
        main._tts_answers.clear()
        main._tts_in_progress.clear()

    def grant(self, user_id):
        token = access_db.create_invite(main.ADMIN_ID, 7)
        status, _ = access_db.activate_invite(
            token, user_id, f"User {user_id}", f"user{user_id}"
        )
        self.assertEqual(status, "activated")

    async def test_closed_commands_are_silent_for_non_admin(self):
        context = SimpleNamespace(args=[], bot=SimpleNamespace())
        handlers = (
            main.cmd_admin,
            main.cmd_invite7,
            main.cmd_users,
            main.cmd_debug,
            main.cmd_id,
        )
        for handler in handlers:
            update = make_update(700, chat_id=main.ADMIN_ID)
            await handler(update, context)
            self.assertEqual(
                update.message.replies,
                [],
                f"{handler.__name__} answered a non-admin",
            )

    async def test_admin_check_uses_effective_user_not_chat(self):
        context = SimpleNamespace(args=[], bot=SimpleNamespace())
        update = make_update(main.ADMIN_ID, chat_id=999999)
        await main.cmd_invite7(update, context)
        self.assertEqual(len(update.message.replies), 1)

    async def test_invitation_hides_technical_url_and_keeps_clickable_text(self):
        context = SimpleNamespace(args=[], bot=SimpleNamespace())
        update = make_update(main.ADMIN_ID)
        await main.cmd_invite7(update, context)

        text, kwargs = update.message.replies[0]
        self.assertEqual(kwargs["parse_mode"], "HTML")
        self.assertTrue(kwargs["disable_web_page_preview"])
        self.assertIn("<b>Принять приглашение</b>", text)
        visible_text = re.sub(r"<[^>]+>", "", text)
        self.assertNotIn("https://t.me/", visible_text)

        button = kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.text, "🚀 Принять приглашение")
        self.assertIn(button.url, text)

    async def test_user_without_access_is_refused(self):
        update = make_update(710)
        await main.handle_text(update, SimpleNamespace())
        self.assertEqual(len(update.message.replies), 1)
        self.assertIn("Доступ", update.message.replies[0][0])

    async def test_active_user_reaches_existing_answer_pipeline(self):
        user_id = 714
        self.grant(user_id)
        update = make_update(user_id, text="Что такое слайд?")
        answer_mock = AsyncMock()
        with patch.object(main, "_answer", answer_mock):
            await main.handle_text(update, SimpleNamespace())
        answer_mock.assert_awaited_once_with(update, "Что такое слайд?")

    async def test_user_without_access_cannot_start_voice_processing(self):
        class ForbiddenBot:
            async def get_file(self, _file_id):
                raise AssertionError("Voice download must not start")

        update = make_update(711)
        update.message.voice = SimpleNamespace(file_id="voice")
        await main.handle_voice(update, SimpleNamespace(bot=ForbiddenBot()))
        self.assertEqual(len(update.message.replies), 1)
        self.assertIn("Доступ", update.message.replies[0][0])

    async def test_start_link_activates_only_first_user(self):
        token = access_db.create_invite(main.ADMIN_ID, 7)
        first = make_update(712)
        await main.cmd_start(first, SimpleNamespace(args=[token]))
        first_expiry = int(access_db.get_access(712)["expires_at"])
        self.assertTrue(access_db.has_active_access(712))

        repeat = make_update(712)
        await main.cmd_start(repeat, SimpleNamespace(args=[token]))
        self.assertEqual(
            int(access_db.get_access(712)["expires_at"]),
            first_expiry,
        )

        second = make_update(713)
        await main.cmd_start(second, SimpleNamespace(args=[token]))
        self.assertFalse(access_db.has_active_access(713))
        self.assertIn("другим человеком", second.message.replies[0][0])

    async def test_revocation_during_query_blocks_ready_answer(self):
        user_id = 720
        self.grant(user_id)
        update = make_update(user_id)

        def revoke_and_answer(*_args):
            access_db.revoke_access(user_id)
            return "Сырой ответ"

        with patch.object(main, "_ask_notebooklm", side_effect=revoke_and_answer):
            await main._answer(update, "Что такое слайд?")

        self.assertEqual([item[0] for item in update.message.replies], [
            "Ищу в материалах метода... ⏳"
        ])

    async def test_revocation_stops_old_tts_button(self):
        user_id = 730
        self.grant(user_id)
        main._tts_answers["oldtoken"] = (user_id, "Старый ответ")
        access_db.revoke_access(user_id)
        update = make_update(user_id, callback_data="tts:oldtoken")

        with patch.object(
            main,
            "_text_to_speech",
            side_effect=AssertionError("TTS must not start"),
        ):
            await main.handle_tts_button(update, SimpleNamespace())

        self.assertEqual(update.message.replies, [])
        self.assertEqual(update.message.voices, [])
        self.assertEqual(len(update.callback_query.answers), 1)

    async def test_long_answer_rechecks_access_between_chunks(self):
        user_id = 740
        self.grant(user_id)

        def revoke_after_first_reply(reply_count):
            if reply_count == 1:
                access_db.revoke_access(user_id)

        update = make_update(user_id)
        update.message.on_reply = revoke_after_first_reply
        sent = await main._send_long(
            update,
            "а" * 5000,
            user_id=user_id,
        )
        self.assertFalse(sent)
        self.assertEqual(len(update.message.replies), 1)

    async def test_command_menus_are_separated(self):
        class FakeBot:
            def __init__(self):
                self.calls = []

            async def set_my_commands(self, commands, **kwargs):
                self.calls.append((commands, kwargs))

        bot = FakeBot()
        await main._configure_bot_commands(SimpleNamespace(bot=bot))
        self.assertEqual(len(bot.calls), 2)

        regular_commands = {command.command for command in bot.calls[0][0]}
        admin_commands = {command.command for command in bot.calls[1][0]}
        self.assertEqual(regular_commands, {"start", "reset", "help"})
        self.assertEqual(
            admin_commands,
            {
                "start",
                "reset",
                "help",
                "admin",
                "invite7",
                "users",
                "debug",
                "id",
            },
        )
        self.assertEqual(bot.calls[1][1]["scope"].chat_id, main.ADMIN_ID)

    async def test_notebooklm_health_alert_is_sent_once_and_recovers(self):
        bot = SimpleNamespace(send_message=AsyncMock())
        context = SimpleNamespace(bot=bot)
        main._nb_health_alert_active = False

        with patch.object(main, "_run_blocking", AsyncMock(return_value=False)):
            await main._periodic_nb_refresh_job(context)
            await main._periodic_nb_refresh_job(context)

        self.assertEqual(bot.send_message.await_count, 1)
        self.assertTrue(main._nb_health_alert_active)

        with patch.object(main, "_run_blocking", AsyncMock(return_value=True)):
            await main._periodic_nb_refresh_job(context)

        self.assertEqual(bot.send_message.await_count, 2)
        self.assertFalse(main._nb_health_alert_active)


if __name__ == "__main__":
    unittest.main(verbosity=2)
