import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from lumen.config import Settings
from lumen.main import create_app


class FakeOllamaConnector:
    def __init__(
        self,
        response_text: str = "Stubbed model answer",
        available: bool = True,
        extraction_response_text: str = "[]",
    ) -> None:
        self.response_text = response_text
        self.available = available
        self.extraction_response_text = extraction_response_text
        self.last_messages = None
        self.last_chat_messages = None
        self.last_extraction_messages = None
        self.model = "fake-model"

    async def chat(self, messages, model=None):
        self.last_messages = messages
        combined = "\n".join(message.get("content", "") for message in messages)
        if "strict JSON information extractor" in combined or "Return only valid JSON." in combined:
            self.last_extraction_messages = messages
            return {"message": {"content": self.extraction_response_text}}
        self.last_chat_messages = messages
        return {"message": {"content": self.response_text}}

    async def healthcheck(self) -> bool:
        return self.available


class FakeHomeAssistantConnector:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.calls = []
        self.list_entities_calls = 0
        self.entities = [
            {"entity_id": "script.turnoffeverything", "state": "off", "attributes": {"friendly_name": "Turn Off Everything"}},
            {"entity_id": "scene.movie_time", "state": "scening", "attributes": {"friendly_name": "Movie Time"}},
            {"entity_id": "input_boolean.guest_mode", "state": "off", "attributes": {"friendly_name": "Guest Mode"}},
            {"entity_id": "light.living_room", "state": "on", "attributes": {"friendly_name": "Living Room Light", "brightness": 180}},
            {"entity_id": "switch.tz3000_w0qqde0g_ts011f", "state": "off", "attributes": {"friendly_name": "Socket 1"}},
            {"entity_id": "sensor.bedroom_temperature", "state": "23", "attributes": {"friendly_name": "Bedroom Temperature", "temperature": 23}},
        ]

    async def execute_service(self, domain, service, service_data):
        self.calls.append((domain, service, service_data))
        return {"ok": True, "domain": domain, "service": service}

    async def list_entities(self):
        self.list_entities_calls += 1
        return self.entities

    async def snapshot_entities(self):
        return {
            "scripts": [entity for entity in self.entities if entity["entity_id"].startswith("script.")],
            "scenes": [entity for entity in self.entities if entity["entity_id"].startswith("scene.")],
            "input_booleans": [entity for entity in self.entities if entity["entity_id"].startswith("input_boolean.")],
        }

    async def healthcheck(self) -> bool:
        return self.available


class LumenApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "lumen-test.db")
        self.knowledge_dir = Path(self.temp_dir.name) / "knowledge"
        self.knowledge_dir.mkdir(parents=True, exist_ok=True)
        settings = Settings(
            lumen_db_path=self.db_path,
            ollama_base_url="",
            home_assistant_url="",
            home_assistant_token="",
            knowledge_paths=str(self.knowledge_dir),
        )
        self.app = create_app(settings)
        self.app.state.container.ollama = FakeOllamaConnector()
        self.app.state.container.agent_service.ollama = self.app.state.container.ollama
        self.app.state.container.home_assistant = FakeHomeAssistantConnector()
        self.app.state.container.agent_service.home_assistant = self.app.state.container.home_assistant
        self.app.state.container.bootstrap_service.home_assistant = self.app.state.container.home_assistant
        self.client = TestClient(self.app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()

    def test_healthcheck(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_system_healthcheck_reports_dependencies(self) -> None:
        response = self.client.get("/health/system")
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["dependencies"]["database"], "ok")
        self.assertEqual(payload["dependencies"]["home_assistant"], "ok")
        self.assertEqual(payload["dependencies"]["ollama"], "ok")

    def test_admin_ui_page_is_served(self) -> None:
        response = self.client.get("/ui")
        self.assertEqual(response.status_code, 200)
        self.assertIn("LUMEN // CORE", response.text)

    def test_admin_summary_reports_counts(self) -> None:
        self.client.post("/chat/ask", json={"text": "I prefer calm music in the evening.", "conversation_id": "summary-1"})
        response = self.client.get("/admin/summary")
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["dependencies"]["database"], "ok")
        self.assertGreaterEqual(payload["counts"]["memory_facts"], 1)
        self.assertIn("current_model", payload)

    def test_host_telemetry_endpoint_reports_ram(self) -> None:
        response = self.client.get("/admin/host/telemetry")
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("ram", payload)
        self.assertIn("disk", payload)

    def test_database_overview_lists_recent_documents(self) -> None:
        (self.knowledge_dir / "overview-note.md").write_text("Kitchen note for knowledge overview.", encoding="utf-8")
        self.client.post("/admin/reindex", json={"paths": [str(self.knowledge_dir)]})
        response = self.client.get("/admin/database/overview?limit=5")
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(payload["table_counts"]["knowledge_documents"], 1)
        self.assertEqual(payload["recent_documents"][0]["title"], "overview-note.md")

    def test_admin_memory_crud(self) -> None:
        create_response = self.client.post(
            "/admin/memory/facts",
            json={
                "category": "preference",
                "subject": "user",
                "predicate": "prefers",
                "value": "quiet evenings",
                "confidence": 0.9,
                "importance": 7,
                "source_ref": "admin:test",
                "tags": ["manual", "preference"],
            },
        )
        create_payload = create_response.json()
        self.assertEqual(create_response.status_code, 200)
        fact_id = create_payload["item"]["id"]

        list_response = self.client.get("/admin/memory/facts?limit=10")
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_response.json()["items"][0]["id"], fact_id)

        update_response = self.client.put(
            f"/admin/memory/facts/{fact_id}",
            json={
                "category": "preference",
                "subject": "user",
                "predicate": "prefers",
                "value": "silent evenings",
                "confidence": 0.95,
                "importance": 8,
                "source_ref": "admin:test-updated",
                "tags": ["manual"],
            },
        )
        self.assertEqual(update_response.status_code, 200)
        self.assertEqual(update_response.json()["item"]["value"], "silent evenings")

        delete_response = self.client.delete(f"/admin/memory/facts/{fact_id}")
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_response.json()["deleted_id"], fact_id)

    def test_admin_memory_create_from_text(self) -> None:
        response = self.client.post(
            "/admin/memory/facts/from-text",
            json={
                "text": "Запам'ятай, що Влад любить тепле світло ввечері.",
            },
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["predicate"], "remember")

    def test_knowledge_document_preview_and_delete(self) -> None:
        note_path = self.knowledge_dir / "preview-delete-note.md"
        note_path.write_text("Knowledge preview body.", encoding="utf-8")
        self.client.post("/admin/reindex", json={"paths": [str(self.knowledge_dir)]})

        overview = self.client.get("/admin/database/overview?limit=10").json()
        document = next(item for item in overview["recent_documents"] if item["title"] == "preview-delete-note.md")

        preview_response = self.client.get(f"/admin/knowledge/documents/{document['id']}")
        self.assertEqual(preview_response.status_code, 200)
        self.assertIn("Knowledge preview body.", preview_response.json()["item"]["content"])

        delete_response = self.client.delete(f"/admin/knowledge/documents/{document['id']}")
        self.assertEqual(delete_response.status_code, 200)
        self.assertEqual(delete_response.json()["deleted_id"], document["id"])
        self.assertFalse(note_path.exists())

    def test_chat_stores_relevant_memory_fact(self) -> None:
        response = self.client.post(
            "/chat/ask",
            json={
                "text": "I prefer warm lights in the evening.",
                "conversation_id": "conv-1",
                "session_id": "sess-1",
                "user_id": "user-1",
            },
        )
        self.assertEqual(response.status_code, 200)
        memory = self.client.post("/memory/search", json={"query": "warm lights", "limit": 10})
        payload = memory.json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["predicate"], "prefers")

    def test_chat_can_store_secret_fact_locally(self) -> None:
        response = self.client.post(
            "/chat/ask",
            json={
                "text": "My wifi password is swordfish42.",
                "conversation_id": "conv-secret-1",
                "session_id": "sess-secret-1",
                "user_id": "user-1",
            },
        )
        self.assertEqual(response.status_code, 200)
        memory = self.client.post("/memory/search", json={"query": "swordfish42", "limit": 10})
        payload = memory.json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["subject"], "wifi password")

    def test_llm_memory_extraction_stores_general_fact(self) -> None:
        fake_ollama = FakeOllamaConnector(
            response_text="Noted.",
            extraction_response_text='[{"category":"routine","subject":"user","predicate":"works_from_home","value":"on Fridays","confidence":0.91,"importance":7,"tags":["routine","work"]}]',
            available=True,
        )
        self.app.state.container.ollama = fake_ollama
        self.app.state.container.agent_service.ollama = fake_ollama
        response = self.client.post(
            "/chat/ask",
            json={
                "text": "Я працюю з дому по п'ятницях.",
                "conversation_id": "conv-llm-memory-1",
                "session_id": "sess-llm-memory-1",
                "user_id": "user-ua",
            },
        )
        self.assertEqual(response.status_code, 200)
        memory = self.client.post("/memory/search", json={"query": "Fridays", "limit": 10})
        payload = memory.json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["predicate"], "works_from_home")

    def test_chat_stores_ukrainian_preference_fact(self) -> None:
        text = "\u042f \u043b\u044e\u0431\u043b\u044e \u0442\u0435\u043f\u043b\u0435 \u0441\u0432\u0456\u0442\u043b\u043e \u0432\u0432\u0435\u0447\u0435\u0440\u0456."
        response = self.client.post(
            "/chat/ask",
            json={
                "text": text,
                "conversation_id": "conv-ua-pref",
                "session_id": "sess-ua-pref",
                "user_id": "user-ua",
            },
        )
        self.assertEqual(response.status_code, 200)
        memory = self.client.post(
            "/memory/search",
            json={"query": "\u0442\u0435\u043f\u043b\u0435 \u0441\u0432\u0456\u0442\u043b\u043e", "limit": 10},
        )
        payload = memory.json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["predicate"], "prefers")
        self.assertIn("\u0442\u0435\u043f\u043b\u0435 \u0441\u0432\u0456\u0442\u043b\u043e", payload[0]["value"])

    def test_chat_stores_ukrainian_name_fact(self) -> None:
        text = "\u041c\u0435\u043d\u0435 \u0437\u0432\u0430\u0442\u0438 \u0412\u043b\u0430\u0434."
        response = self.client.post(
            "/chat/ask",
            json={
                "text": text,
                "conversation_id": "conv-ua-name",
                "session_id": "sess-ua-name",
                "user_id": "user-ua",
            },
        )
        self.assertEqual(response.status_code, 200)
        memory = self.client.post("/memory/search", json={"query": "\u0412\u043b\u0430\u0434", "limit": 10})
        payload = memory.json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["subject"], "user_name")
        self.assertEqual(payload[0]["value"], "\u0412\u043b\u0430\u0434")

    def test_chat_stores_ukrainian_remember_fact(self) -> None:
        text = "\u0417\u0430\u043f\u0430\u043c'\u044f\u0442\u0430\u0439, \u0449\u043e \u044f \u043f\u0440\u0430\u0446\u044e\u044e \u0437 \u0434\u043e\u043c\u0443 \u043f\u043e \u043f'\u044f\u0442\u043d\u0438\u0446\u044f\u0445."
        response = self.client.post(
            "/chat/ask",
            json={
                "text": text,
                "conversation_id": "conv-ua-rule",
                "session_id": "sess-ua-rule",
                "user_id": "user-ua",
            },
        )
        self.assertEqual(response.status_code, 200)
        memory = self.client.post(
            "/memory/search",
            json={"query": "\u043f\u0440\u0430\u0446\u044e\u044e \u0437 \u0434\u043e\u043c\u0443", "limit": 10},
        )
        payload = memory.json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["predicate"], "remember")

    def test_chat_does_not_store_noise_fact(self) -> None:
        response = self.client.post("/chat/ask", json={"text": "hi", "conversation_id": "conv-2"})
        self.assertEqual(response.status_code, 200)
        memory = self.client.post("/memory/search", json={"query": "hi", "limit": 10})
        self.assertEqual(memory.json(), [])

    def test_plain_assist_chat_does_not_fetch_live_home_state(self) -> None:
        response = self.client.post(
            "/assist/process",
            json={"text": "Привіт! Як справи?", "conversation_id": "assist-smalltalk", "user_id": "user-1", "allow_actions": False},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.app.state.container.home_assistant.list_entities_calls, 0)

    def test_home_assistant_question_fetches_relevant_live_state(self) -> None:
        fake_ollama = FakeOllamaConnector(response_text="Here is the live state", available=True)
        self.app.state.container.ollama = fake_ollama
        self.app.state.container.agent_service.ollama = fake_ollama
        response = self.client.post(
            "/assist/process",
            json={"text": "Яка зараз температура в спальні?", "conversation_id": "assist-live-state", "user_id": "user-1", "allow_actions": False},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.app.state.container.home_assistant.list_entities_calls, 1)
        prompt_dump = "\n".join(message["content"] for message in fake_ollama.last_chat_messages)
        self.assertIn("Live Home Assistant state:", prompt_dump)
        self.assertIn("Bedroom Temperature", prompt_dump)

    def test_inventory_intent_returns_deterministic_entity_list(self) -> None:
        fake_ollama = FakeOllamaConnector(response_text="Hallucinated summary", available=True)
        self.app.state.container.ollama = fake_ollama
        self.app.state.container.agent_service.ollama = fake_ollama
        response = self.client.post(
            "/assist/process",
            json={"text": "дай перелік всіх пристроїв які ти бачиш", "conversation_id": "assist-inventory", "user_id": "user-1", "allow_actions": False},
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertIn("Я бачу", payload["speech"])
        self.assertIn("light: 1", payload["speech"])
        self.assertIn("Приклади:", payload["speech"])
        self.assertNotIn("Hallucinated summary", payload["speech"])
        self.assertLess(len(payload["speech"]), 1200)

    def test_device_alias_is_used_in_inventory_list(self) -> None:
        response = self.client.post(
            "/chat/ask",
            json={
                "text": "light.living_room це світло біля дивану.",
                "conversation_id": "alias-1",
                "session_id": "alias-1",
                "user_id": "user-1",
            },
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            "/assist/process",
            json={"text": "що ти бачиш", "conversation_id": "assist-alias", "user_id": "user-1", "allow_actions": False},
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertIn("світло біля дивану", payload["speech"])

    def test_assist_process_escapes_markdown_v2_special_chars(self) -> None:
        fake_ollama = FakeOllamaConnector(response_text="Status: *ok* [link](x) _test_!", available=True)
        self.app.state.container.ollama = fake_ollama
        self.app.state.container.agent_service.ollama = fake_ollama
        response = self.client.post(
            "/assist/process",
            json={"text": "Що зі статусом?", "conversation_id": "assist-markdown", "user_id": "user-1", "allow_actions": False},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["speech"], r"Status: \*ok\* \[link\]\(x\) \_test\_\!")

    def test_knowledge_reindex_and_search(self) -> None:
        (self.knowledge_dir / "guest-rule.md").write_text(
            "Guest mode keeps hallway lights on and relaxes bedtime automations.",
            encoding="utf-8",
        )
        response = self.client.post("/admin/reindex", json={"paths": [str(self.knowledge_dir)]})
        self.assertEqual(response.status_code, 200)
        search = self.client.post("/knowledge/search", json={"query": "guest mode hallway", "limit": 5})
        payload = search.json()
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["source_type"], "house_rule")

    def test_knowledge_upload_writes_file_and_indexes_it(self) -> None:
        response = self.client.post(
            "/admin/knowledge/upload",
            json={
                "filename": "manual-note.md",
                "content": "Guest mode should keep the corridor lights on.",
                "relative_path": "notes",
                "reindex_after_upload": True,
            },
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue((self.knowledge_dir / "notes" / "manual-note.md").exists())
        self.assertEqual(payload["indexed_documents"], 1)

        search = self.client.post("/knowledge/search", json={"query": "corridor lights", "limit": 5})
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.json()[0]["title"], "manual-note.md")

    def test_action_requires_confirmation(self) -> None:
        response = self.client.post(
            "/chat/ask",
            json={"text": "Увімкни гостьовий режим", "conversation_id": "conv-3", "user_id": "user-1"},
        )
        payload = response.json()
        self.assertTrue(payload["requires_confirmation"])
        self.assertEqual(payload["action_proposal"]["ha_domain"], "input_boolean")

    def test_confirm_action_rejects_unknown_id(self) -> None:
        response = self.client.post(
            "/chat/confirm-action",
            json={"action_id": "missing", "confirmed": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("No pending action", response.json()["answer"])

    def test_confirm_action_executes_allowlisted_service(self) -> None:
        proposal = self.client.post(
            "/chat/ask",
            json={"text": "Turn off everything", "conversation_id": "conv-4", "user_id": "user-1"},
        ).json()["action_proposal"]
        response = self.client.post(
            "/chat/confirm-action",
            json={"action_id": proposal["action_id"], "confirmed": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Action executed", response.json()["answer"])
        self.assertEqual(len(self.app.state.container.home_assistant.calls), 1)

    def test_generic_light_action_requires_confirmation(self) -> None:
        response = self.client.post(
            "/chat/ask",
            json={"text": "turn off the living room light", "conversation_id": "conv-light-1", "user_id": "user-1"},
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["requires_confirmation"])
        self.assertEqual(payload["action_proposal"]["ha_domain"], "light")
        self.assertEqual(payload["action_proposal"]["ha_service"], "turn_off")
        self.assertEqual(payload["action_proposal"]["service_data"]["entity_id"], "light.living_room")

    def test_confirm_action_executes_generic_light_service(self) -> None:
        proposal = self.client.post(
            "/chat/ask",
            json={"text": "turn off the living room light", "conversation_id": "conv-light-2", "user_id": "user-1"},
        ).json()["action_proposal"]
        response = self.client.post(
            "/chat/confirm-action",
            json={"action_id": proposal["action_id"], "confirmed": True},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Action executed", response.json()["answer"])
        self.assertEqual(
            self.app.state.container.home_assistant.calls[-1],
            ("light", "turn_off", {"entity_id": "light.living_room"}),
        )

    def test_ollama_messages_are_used_when_available(self) -> None:
        fake_ollama = FakeOllamaConnector(response_text="Ollama answered", available=True)
        self.app.state.container.ollama = fake_ollama
        self.app.state.container.agent_service.ollama = fake_ollama
        response = self.client.post("/chat/ask", json={"text": "What do you know?", "conversation_id": "conv-5"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"], "Ollama answered")
        self.assertIsNotNone(fake_ollama.last_messages)

    def test_recent_conversation_is_included_in_followup_prompt(self) -> None:
        fake_ollama = FakeOllamaConnector(response_text="Context-aware answer", available=True)
        self.app.state.container.ollama = fake_ollama
        self.app.state.container.agent_service.ollama = fake_ollama

        first = self.client.post(
            "/chat/ask",
            json={"text": "The charger plug is Socket 1.", "conversation_id": "conv-history-1", "user_id": "user-1"},
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            "/chat/ask",
            json={"text": "What was the plug called?", "conversation_id": "conv-history-1", "user_id": "user-1"},
        )
        self.assertEqual(second.status_code, 200)
        prompt_dump = "\n".join(message["content"] for message in fake_ollama.last_chat_messages)
        self.assertIn("Recent conversation:", prompt_dump)
        self.assertIn("- user: The charger plug is Socket 1.", prompt_dump)
        self.assertIn("- assistant: Context-aware answer", prompt_dump)

    def test_home_assistant_snapshot_endpoint(self) -> None:
        response = self.client.get("/home-assistant/snapshot")
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["groups"]["scripts"], 1)
        self.assertEqual(payload["groups"]["input_booleans"], 1)

    def test_bootstrap_home_assistant_indexes_snapshot(self) -> None:
        response = self.client.post("/admin/bootstrap-home-assistant")
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "ok")
        search = self.client.post("/knowledge/search", json={"query": "guest mode", "limit": 10})
        self.assertTrue(len(search.json()) >= 1)

    def test_sleep_mode_is_allowlisted(self) -> None:
        response = self.client.post(
            "/chat/ask",
            json={"text": "enable sleep mode", "conversation_id": "conv-6", "user_id": "user-1"},
        )
        payload = response.json()
        self.assertTrue(payload["requires_confirmation"])
        self.assertEqual(payload["action_proposal"]["service_data"]["entity_id"], "input_boolean.sleeping")

    def test_assist_process_returns_confirmation_shape(self) -> None:
        response = self.client.post(
            "/assist/process",
            json={
                "text": "turn on guest mode",
                "conversation_id": "assist-1",
                "user_id": "user-1",
                "exposed_entities": ["input_boolean.guest_mode", "script.turnoffeverything"],
            },
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["response_type"], "action_confirmation")
        self.assertTrue(payload["requires_confirmation"])
        self.assertEqual(payload["action_label"], "Enable guest mode")
        self.assertIn("action_proposal", payload["data"])

    def test_assist_confirm_executes_action(self) -> None:
        proposal = self.client.post(
            "/assist/process",
            json={"text": "turn off everything", "conversation_id": "assist-2", "user_id": "user-1"},
        ).json()
        response = self.client.post(
            "/assist/confirm",
            json={
                "action_id": proposal["action_id"],
                "confirmed": True,
                "conversation_id": "assist-2",
                "user_id": "user-1",
            },
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["response_type"], "action_result")
        self.assertIn("Action executed", payload["speech"])

    def test_assist_process_builds_generic_light_action(self) -> None:
        response = self.client.post(
            "/assist/process",
            json={"text": "turn off the living room light", "conversation_id": "assist-light-1", "user_id": "user-1"},
        )
        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["response_type"], "action_confirmation")
        self.assertTrue(payload["requires_confirmation"])
        self.assertEqual(payload["data"]["action_proposal"]["ha_domain"], "light")
        self.assertEqual(payload["data"]["action_proposal"]["ha_service"], "turn_off")
        self.assertEqual(payload["data"]["action_proposal"]["service_data"]["entity_id"], "light.living_room")

    def test_assist_process_resolves_followup_pronoun_to_recent_entity(self) -> None:
        first = self.client.post(
            "/assist/process",
            json={"text": "switch.tz3000_w0qqde0g_ts011f", "conversation_id": "assist-switch-followup", "user_id": "user-1"},
        )
        self.assertEqual(first.status_code, 200)

        second = self.client.post(
            "/assist/process",
            json={"text": "turn it on", "conversation_id": "assist-switch-followup", "user_id": "user-1"},
        )
        payload = second.json()
        self.assertEqual(second.status_code, 200)
        self.assertEqual(payload["response_type"], "action_confirmation")
        self.assertTrue(payload["requires_confirmation"])
        self.assertEqual(payload["data"]["action_proposal"]["ha_domain"], "switch")
        self.assertEqual(payload["data"]["action_proposal"]["ha_service"], "turn_on")
        self.assertEqual(
            payload["data"]["action_proposal"]["service_data"]["entity_id"],
            "switch.tz3000_w0qqde0g_ts011f",
        )

if __name__ == "__main__":
    unittest.main()
