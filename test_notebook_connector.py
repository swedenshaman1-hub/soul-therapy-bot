import base64
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from notebook_connector import NotebookConnector, NotebookConnectorError


AUTH = {
    "cookies": {
        "SID": "sid",
        "HSID": "hsid",
        "SSID": "ssid",
        "APISID": "apisid",
        "SAPISID": "sapisid",
    },
    "csrf_token": "csrf",
    "session_id": "session",
}


def completed(payload):
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps(payload, ensure_ascii=False),
        stderr="",
    )


class NotebookConnectorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        encoded = base64.b64encode(
            json.dumps(AUTH).encode("utf-8")
        ).decode("ascii")
        self.env = patch.dict(
            os.environ,
            {
                "NOTEBOOKLM_AUTH_JSON_B64": encoded,
                "NOTEBOOKLM_AUTH_JSON": "",
                "NOTEBOOKLM_MCP_DATA_DIR": self.temp_dir.name,
                "NOTEBOOKLM_BL": "",
                "NOTEBOOKLM_AUTO_METADATA": "0",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def test_b64_auth_is_persisted_for_mcp_compatibility(self):
        NotebookConnector("notebook-id")
        auth_path = os.path.join(self.temp_dir.name, "auth.json")
        self.assertTrue(os.path.exists(auth_path))
        with open(auth_path, encoding="utf-8") as source:
            self.assertEqual(json.load(source)["cookies"]["SID"], "sid")

    def test_missing_auth_fails_closed(self):
        with patch.dict(
            os.environ,
            {
                "NOTEBOOKLM_AUTH_JSON_B64": "",
                "NOTEBOOKLM_AUTH_JSON": "",
            },
        ):
            with self.assertRaises(NotebookConnectorError):
                NotebookConnector("notebook-id")

    @patch("notebook_connector.subprocess.run")
    def test_sources_are_cached_and_answer_is_returned(self, run):
        run.side_effect = [
            completed({
                "status": "success",
                "_source_ids": ["s1", "s2"],
                "_timings": {"sources": 0.5},
            }),
            completed({
                "status": "success",
                "answer": "Ответ из материалов",
                "conversation_id": "c1",
                "_source_ids": ["s1", "s2"],
                "_timings": {"query": 1.2},
            }),
            completed({
                "status": "success",
                "answer": "Продолжение",
                "conversation_id": "c1",
                "_source_ids": ["s1", "s2"],
                "_timings": {"query": 1.0},
            }),
        ]
        connector = NotebookConnector("notebook-id")

        self.assertEqual(connector.query("Вопрос", 10), "Ответ из материалов")
        self.assertEqual(connector.query("Ещё вопрос", 10), "Продолжение")
        self.assertEqual(connector.source_count, 2)
        self.assertEqual(run.call_count, 3)

        second_payload = json.loads(run.call_args_list[2].kwargs["input"])
        self.assertEqual(second_payload["conversation_id"], "c1")
        self.assertEqual(second_payload["source_ids"], ["s1", "s2"])
        self.assertEqual(second_payload["base_url"], "https://notebook.google.com")

    @patch("notebook_connector.subprocess.run")
    def test_preflight_reports_unauthorized_without_hanging(self, run):
        run.return_value = completed({
            "status": "error",
            "error": "HTTP 401 Unauthorized",
        })
        connector = NotebookConnector("notebook-id")

        self.assertFalse(connector.verify_sources(force=True))
        self.assertIn("401", connector.last_error)

    @patch("notebook_connector.subprocess.run")
    def test_preflight_refreshes_metadata_and_retries_unauthorized(self, run):
        run.side_effect = [
            completed({"status": "error", "error": "HTTP 401 Unauthorized"}),
            completed({
                "status": "success",
                "_source_ids": ["s1", "s2"],
                "_timings": {"sources": 0.6},
            }),
        ]
        connector = NotebookConnector("notebook-id")

        with patch.object(
            connector,
            "_refresh_frontend_metadata",
            return_value=True,
        ) as refresh:
            self.assertTrue(connector.verify_sources(force=True))

        refresh.assert_called_once_with()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(connector.source_count, 2)

    def test_auth_expiry_variants_are_detected(self):
        self.assertTrue(NotebookConnector._is_auth_error("RPC Error 16: authentication expired"))
        self.assertTrue(NotebookConnector._is_auth_error("HTTP 401 Unauthorized"))
        self.assertFalse(NotebookConnector._is_auth_error("temporary timeout"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
