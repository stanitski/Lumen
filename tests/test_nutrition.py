from __future__ import annotations

import asyncio
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from lumen.config import Settings
from lumen.models import ConnectorFile, ConnectorRequest, ConnectorReplyTarget
from lumen.services.nutrition import NutritionService
from lumen.storage.nutrition import message_key


def analysis(action="record", *, kcal=600, fraction=1, description='Гречка, курка «гриль»', **kwargs):
    estimate = {"description": description, "full_portion_kcal": kcal, "consumed_fraction": fraction}
    return {"action": action, "estimate": estimate if action in {"record", "update"} else None, **kwargs}


class NutritionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = Settings(_env_file=None, nutrition_directory=self.directory.name,
                                 home_assistant_url="https://ha.example:8123", home_assistant_token="test-token",
                                 ollama_model="configured-vision", ollama_dialogue_model="")
        self.ollama = AsyncMock()
        self.delivery = AsyncMock()
        self.service = NutritionService(self.settings, self.ollama, self.delivery)
        self.service.download_image = AsyncMock(return_value=(b"\xff\xd8\xffphoto", "image/jpeg"))

    def enqueue(self, *results):
        self.ollama.chat_multimodal.side_effect = [
            {"message": {"content": json.dumps(result, ensure_ascii=False)}} for result in results
        ]

    def event(self, mid=1, *, text="", photo=True, reply=None, chat="10", sent_at="2026-10-08T10:23:00Z", **data):
        return ConnectorRequest(source="telegram", event_type="attachment" if photo else "text",
                                user_id="test", reply=ConnectorReplyTarget(target=chat), text=text,
                                file=ConnectorFile(name="food.jpg", mime_type="image/jpeg", url="https://ha.example:8123/local/photo.jpg") if photo else None,
                                data={"message_id": mid, "reply_to_message_id": reply, "sent_at": sent_at, **data})

    async def handle(self, payload, *, user="test", service=None):
        return await (service or self.service).handle(payload, user_id=user, request_id="req",
                                                      callback_metadata={"target_channel": "telegram", "target_chat_id": str(payload.reply.target)})

    def state(self):
        return self.service.store.load("test")

    def rows(self):
        path = self.service.store.paths("test")[1]
        with path.open(encoding="utf-8-sig", newline="") as file:
            return list(csv.reader(file))

    async def test_photo_caption_csv_and_local_time(self):
        self.enqueue(analysis())
        self.assertTrue(await self.handle(self.event(text="Гречка з куркою")))
        self.assertEqual([['дата', 'час', 'калорії', 'опис'], ['2026-10-08', '13:23', '600', 'Гречка, курка «гриль»']], self.rows())
        self.assertTrue(self.service.store.paths("test")[1].read_bytes().startswith(b"\xef\xbb\xbf"))
        call = self.ollama.chat_multimodal.call_args.kwargs
        self.assertEqual("configured-vision", call["model"])
        self.assertEqual("Гречка з куркою", json.loads(call["messages"][1].content)["text"])
        self.assertEqual("image", call["messages"][1].parts[0].type)
        self.assertFalse(call["include_runtime_context"])
        self.assertFalse(call["think"])
        self.assertIn("estimate", call["response_format"]["required"])
        delivered = self.delivery.deliver_chat_answer.call_args.kwargs
        self.assertFalse(delivered["voice"])
        self.assertEqual(1, delivered["metadata"]["reply_to_message_id"])
        self.assertTrue(delivered["metadata"]["message_tag"].startswith("nutrition:"))

    async def test_text_creates_first_meal_without_photo_and_deduplicates(self):
        self.enqueue(analysis(description="Кава з молоком і два печива", kcal=350))
        event = self.event(photo=False, text="Додай каву з молоком і два печива")
        await self.handle(event)
        await self.handle(event)
        self.assertEqual(1, len(self.state().meals))
        self.assertEqual("350", self.rows()[1][2])
        self.service.download_image.assert_not_awaited()
        self.ollama.chat_multimodal.assert_awaited_once()

    async def test_new_text_meal_does_not_overwrite_previous_meal_or_time(self):
        self.enqueue(analysis(), analysis(kcal=350))
        await self.handle(self.event())
        await self.handle(self.event(2, photo=False, text="Додай каву і печиво", sent_at="2026-10-09T09:00:00Z"))
        self.assertEqual([600, 350], [m.estimate.calories for m in self.state().meals])
        self.assertEqual(["2026-10-09", "12:00"], self.rows()[2][:2])

    async def test_new_text_clarification_does_not_target_existing_meal(self):
        self.enqueue(analysis(), analysis("clarify", question="Скільки печива?", new_entry=True), analysis("update", kcal=350))
        await self.handle(self.event())
        await self.handle(self.event(2, photo=False, text="Додай каву з печивом"))
        pending = next(iter(self.state().pending.values()))
        self.assertIsNone(pending.target_meal_id)
        await self.handle(self.event(3, photo=False, reply=2, text="Два печива"))
        self.assertEqual([600, 350], [m.estimate.calories for m in self.state().meals])

    async def test_text_other_day_requires_time(self):
        self.enqueue(analysis(meal_date="2026-10-07"), analysis("update", meal_time="19:00:00"))
        await self.handle(self.event(photo=False, text="Запиши вчорашню каву"))
        self.assertEqual([], self.state().meals)
        await self.handle(self.event(2, photo=False, reply=1, text="О 19:00"))
        self.assertEqual(["2026-10-07", "19:00"], self.rows()[1][:2])

    async def test_photo_without_caption(self):
        self.enqueue(analysis())
        await self.handle(self.event())
        self.assertEqual(1, len(self.state().meals))

    async def test_nonfood_and_duplicate_do_not_create_meal(self):
        self.enqueue(analysis("not_food"))
        await self.handle(self.event())
        await self.handle(self.event())
        self.assertEqual([], self.state().meals)
        self.ollama.chat_multimodal.assert_awaited_once()
        self.delivery.deliver_chat_answer.assert_awaited_once()

    async def test_latest_correction_preserves_time_and_full_portion(self):
        self.enqueue(analysis(), analysis("update", fraction=.5, description="Гречка з куркою, половина"))
        await self.handle(self.event())
        await self.handle(self.event(2, photo=False, text="З'їв лише половину", sent_at="2026-10-09T08:00:00Z"))
        self.assertEqual(1, len(self.state().meals))
        self.assertEqual(['2026-10-08', '13:23', '300', 'Гречка з куркою, половина'], self.rows()[1])
        target = json.loads(self.ollama.chat_multimodal.call_args.kwargs["messages"][1].content)["target"]
        self.assertEqual(600, target["estimate"]["full_portion_kcal"])

    async def test_reply_to_old_photo_or_confirmation_selects_old_meal(self):
        self.enqueue(analysis(kcal=600), analysis(kcal=400), analysis("update", kcal=600, fraction=.5))
        await self.handle(self.event())
        tag = self.delivery.deliver_chat_answer.call_args.kwargs["metadata"]["message_tag"]
        sent = self.event(50, photo=False, message_tag=tag)
        sent.event_type = "nutrition_delivery"
        self.assertTrue(await self.handle(sent))
        await self.handle(self.event(2))
        await self.handle(self.event(3, photo=False, reply=50, text="лише половину"))
        self.assertEqual([300, 400], [meal.estimate.calories for meal in self.state().meals])
        # Source photo remains a valid target too.
        self.enqueue(analysis("update", kcal=600, fraction=.25))
        await self.handle(self.event(4, photo=False, reply=1, text="чверть"))
        self.assertEqual([150, 400], [meal.estimate.calories for meal in self.state().meals])

    async def test_unknown_reply_never_falls_back_to_latest(self):
        self.enqueue(analysis())
        await self.handle(self.event())
        self.assertFalse(await self.handle(self.event(2, photo=False, reply=999, text="половину")))
        self.ollama.chat_multimodal.assert_awaited_once()

    async def test_cross_chat_and_user_reply_are_isolated(self):
        self.enqueue(analysis())
        await self.handle(self.event())
        self.assertFalse(await self.handle(self.event(2, photo=False, chat="20", reply=1, text="половину")))
        self.assertFalse(await self.handle(self.event(2, photo=False, reply=1, text="половину"), user="another"))
        self.assertEqual(600, self.state().meals[0].estimate.calories)

    async def test_ordinary_text_is_not_swallowed(self):
        self.enqueue(analysis("unrelated"))
        self.assertFalse(await self.handle(self.event(photo=False, text="привіт")))
        self.enqueue(analysis(), analysis("unrelated"))
        await self.handle(self.event())
        self.assertFalse(await self.handle(self.event(2, photo=False, text="яка погода?")))
        self.assertEqual(1, len(self.state().receipts))

    async def test_csv_failure_does_not_interrupt_unrelated_chat(self):
        self.enqueue(analysis(), analysis("unrelated"))
        await self.handle(self.event())
        with patch.object(self.service.store, "export", side_effect=PermissionError("locked CSV")):
            self.assertFalse(await self.handle(self.event(2, photo=False, text="привіт")))
        self.delivery.deliver_chat_answer.assert_awaited_once()

    async def test_ambiguous_food_waits_then_records_original_timestamp(self):
        self.enqueue(analysis("clarify", question="Що це за страва?", context="Біла страва у мисці"),
                     analysis("update", description="Сир зі сметаною"))
        await self.handle(self.event())
        self.assertEqual([], self.state().meals)
        await self.handle(self.event(2, photo=False, text="Це сир зі сметаною", sent_at="2026-10-09T10:00:00Z"))
        self.assertEqual("2026-10-08", self.rows()[1][0])
        self.assertEqual("13:23", self.rows()[1][1])
        self.assertEqual({}, self.state().pending)

    async def test_other_day_requires_time_then_completes(self):
        self.enqueue(analysis(meal_date="2026-10-07"), analysis("update", meal_time="19:40:00"))
        await self.handle(self.event(text="Вчорашня вечеря"))
        self.assertEqual([], self.state().meals)
        self.assertIn("О котрій", self.delivery.deliver_chat_answer.call_args.kwargs["answer"])
        await self.handle(self.event(2, photo=False, text="о 19:40", reply=1))
        self.assertEqual(["2026-10-07", "19:40"], self.rows()[1][:2])

    async def test_correction_date_clarification_preserves_existing_row_until_complete(self):
        self.enqueue(analysis(), analysis("update", meal_date="2026-10-06"),
                     analysis("update", meal_time="18:00:00"))
        await self.handle(self.event())
        await self.handle(self.event(2, photo=False, text="це було позавчора"))
        self.assertEqual(["2026-10-08", "13:23"], self.rows()[1][:2])
        await self.handle(self.event(3, photo=False, text="о 18", reply=2))
        self.assertEqual(1, len(self.state().meals))
        self.assertEqual(["2026-10-06", "18:00"], self.rows()[1][:2])

    async def test_correction_pending_keeps_old_meal_time(self):
        self.enqueue(analysis(meal_date="2026-10-07", meal_time="18:00:00"),
                     analysis("clarify", question="Яка частка?"), analysis("update", fraction=.5))
        await self.handle(self.event())
        await self.handle(self.event(2, photo=False, text="не все з'їв"))
        await self.handle(self.event(3, photo=False, text="половину"))
        self.assertEqual(["2026-10-07", "18:00", "300"], self.rows()[1][:3])

    async def test_parallel_duplicate_is_analyzed_and_saved_once(self):
        self.enqueue(analysis())
        await asyncio.gather(*(self.handle(self.event()) for _ in range(4)))
        self.assertEqual(1, len(self.state().meals))
        self.ollama.chat_multimodal.assert_awaited_once()
        self.delivery.deliver_chat_answer.assert_awaited_once()

    async def test_parallel_distinct_photos_are_both_preserved(self):
        self.enqueue(analysis(kcal=500), analysis(kcal=700))
        await asyncio.gather(self.handle(self.event(1)), self.handle(self.event(2)))
        self.assertEqual([500, 700], [meal.estimate.calories for meal in self.state().meals])

    async def test_same_second_default_targets_last_photo(self):
        self.enqueue(analysis(kcal=500), analysis(kcal=700), analysis("update", kcal=700, fraction=.5))
        await self.handle(self.event(1))
        await self.handle(self.event(2))
        await self.handle(self.event(3, photo=False, text="половину"))
        self.assertEqual([500, 350], [meal.estimate.calories for meal in self.state().meals])

    async def test_callback_failure_retry_reuses_saved_estimate(self):
        self.enqueue(analysis())
        self.delivery.deliver_chat_answer.side_effect = RuntimeError("delivery failed")
        await self.handle(self.event())
        self.assertFalse(self.state().receipts[message_key("10", "1")].callback_accepted)
        self.delivery.deliver_chat_answer.side_effect = None
        await self.handle(self.event())
        self.assertTrue(self.state().receipts[message_key("10", "1")].callback_accepted)
        self.assertEqual(1, len(self.state().meals))
        self.ollama.chat_multimodal.assert_awaited_once()

    async def test_pending_and_confirmation_reply_survive_restart(self):
        self.enqueue(analysis("clarify", question="Що в мисці?"), analysis("update"))
        await self.handle(self.event())
        tag = self.delivery.deliver_chat_answer.call_args.kwargs["metadata"]["message_tag"]
        self.assertEqual("test", json.loads(tag.removeprefix("nutrition:"))["user_id"])
        sent = self.event(50, photo=False, message_tag=tag)
        sent.event_type = "nutrition_delivery"
        await self.handle(sent)
        restarted = NutritionService(self.settings, self.ollama, self.delivery)
        await self.handle(self.event(2, photo=False, reply=50, text="Гречка"), service=restarted)
        self.assertEqual(1, len(self.state().meals))

    async def test_timestamp_crosses_local_midnight(self):
        self.enqueue(analysis())
        await self.handle(self.event(sent_at="2026-10-08T22:30:00Z"))
        self.assertEqual(["2026-10-09", "01:30"], self.rows()[1][:2])

    async def test_cancellation_does_not_save_partial_model_result(self):
        self.ollama.chat_multimodal.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.handle(self.event())
        self.assertFalse(self.service.store.paths("test")[0].exists())
        self.delivery.deliver_chat_answer.assert_not_awaited()

    async def test_restart_recovers_csv_and_reply_metadata(self):
        self.enqueue(analysis(), analysis("update", fraction=.5))
        await self.handle(self.event())
        self.service.store.paths("test")[1].unlink()
        restarted = NutritionService(self.settings, self.ollama, self.delivery)
        await self.handle(self.event(2, photo=False, reply=1, text="половину"), service=restarted)
        self.assertEqual(300, self.state().meals[0].estimate.calories)
        self.assertEqual(2, len(self.rows()))

    async def test_csv_failure_never_sends_success_and_retry_does_not_duplicate(self):
        self.enqueue(analysis())
        with patch.object(self.service.store, "export", side_effect=PermissionError("Excel has opened CSV")):
            await self.handle(self.event())
        self.assertNotIn("Записав:", self.delivery.deliver_chat_answer.call_args.kwargs["answer"])
        await self.handle(self.event())
        self.assertEqual(1, len(self.state().meals))
        self.ollama.chat_multimodal.assert_awaited_once()
        self.assertIn("Записав:", self.delivery.deliver_chat_answer.call_args.kwargs["answer"])

    async def test_json_failure_keeps_previous_state(self):
        self.enqueue(analysis(), analysis("update", fraction=.5))
        await self.handle(self.event())
        path = self.service.store.paths("test")[0]
        before = path.read_bytes()
        with patch.object(self.service.store, "save", side_effect=OSError("disk full")):
            await self.handle(self.event(2, photo=False, text="половина"))
        self.assertEqual(before, path.read_bytes())
        self.assertEqual(600, self.state().meals[0].estimate.calories)

    async def test_invalid_model_output_or_download_does_not_write(self):
        for invalid in ('not json', '{"action":"record"}', json.dumps(analysis(kcal=-1)), json.dumps(analysis(kcal=600.5))):
            with self.subTest(invalid=invalid):
                self.ollama.chat_multimodal.side_effect = None
                self.ollama.chat_multimodal.return_value = {"message": {"content": invalid}}
                await self.handle(self.event())
                self.assertFalse(self.service.store.paths("test")[0].exists())
        self.service.download_image.side_effect = ValueError("bad photo")
        await self.handle(self.event())
        self.assertFalse(self.service.store.paths("test")[0].exists())

    async def test_download_failure_reports_stage_without_csv_advice_or_secrets(self):
        self.service.download_image.side_effect = ValueError("secret URL")
        with self.assertLogs("lumen", level="WARNING") as logs:
            await self.handle(self.event())
        self.assertIn("stage=download", " ".join(logs.output))
        self.assertNotIn("secret URL", " ".join(logs.output))
        answer = self.delivery.deliver_chat_answer.call_args.kwargs["answer"]
        self.assertIn("завантажити фото", answer)
        self.assertNotIn("Excel", answer)
        self.ollama.chat_multimodal.assert_not_awaited()
        self.assertFalse(self.service.store.paths("test")[0].exists())

    async def test_model_failure_reports_analysis_without_csv_advice(self):
        self.ollama.chat_multimodal.side_effect = RuntimeError("private model data")
        await self.handle(self.event())
        answer = self.delivery.deliver_chat_answer.call_args.kwargs["answer"]
        self.assertIn("оцінку їжі", answer)
        self.assertNotIn("Excel", answer)
        self.assertFalse(self.service.store.paths("test")[0].exists())

    async def test_corrupt_json_is_not_overwritten(self):
        path = self.service.store.paths("test")[0]
        path.write_text("broken", encoding="utf-8")
        await self.handle(self.event())
        self.assertEqual("broken", path.read_text(encoding="utf-8"))
        self.ollama.chat_multimodal.assert_not_awaited()

    async def test_missing_metadata_no_guessing(self):
        for field in ("message_id", "sent_at"):
            event = self.event()
            event.data.pop(field)
            await self.handle(event)
            self.assertFalse(self.service.store.paths("test")[0].exists())
        event = self.event()
        event.data["sent_at"] = "2026-10-08T13:23:00"
        await self.handle(event)
        self.assertFalse(self.service.store.paths("test")[0].exists())

    async def test_non_telegram_and_stt_are_not_intercepted(self):
        event = self.event()
        event.source = "api"
        self.assertFalse(await self.handle(event))
        event.source = "telegram"
        event.file = ConnectorFile(name="voice.ogg", mime_type="audio/ogg")
        self.assertFalse(await self.handle(event))

    async def test_zero_consumed_fraction_and_formula_safe_csv(self):
        self.enqueue(analysis(description="=formula"), analysis("update", fraction=0))
        await self.handle(self.event())
        self.assertEqual("'=formula", self.rows()[1][3])
        await self.handle(self.event(2, photo=False, text="нічого не їв"))
        self.assertEqual("0", self.rows()[1][2])

    async def test_custom_timezone_and_nutrition_model(self):
        self.settings.nutrition_timezone = "UTC"
        self.settings.nutrition_model = "vision-override"
        service = NutritionService(self.settings, self.ollama, self.delivery)
        service.download_image = self.service.download_image
        self.enqueue(analysis())
        await self.handle(self.event(), service=service)
        self.assertEqual("10:23", self.rows()[1][1])
        self.assertEqual("vision-override", self.ollama.chat_multimodal.call_args.kwargs["model"])

    async def test_same_message_id_in_different_chats_is_not_duplicate(self):
        self.enqueue(analysis(kcal=500), analysis(kcal=700))
        await self.handle(self.event(chat="10"))
        await self.handle(self.event(chat="20"))
        self.assertEqual(2, len(self.state().meals))

    async def test_sent_event_cannot_bind_another_chat(self):
        self.enqueue(analysis())
        await self.handle(self.event())
        tag = self.delivery.deliver_chat_answer.call_args.kwargs["metadata"]["message_tag"]
        event = self.event(50, photo=False, chat="20", message_tag=tag)
        event.event_type = "nutrition_delivery"
        self.assertFalse(await self.handle(event))
        self.assertNotIn(message_key("20", "50"), self.state().message_refs)


class ImageDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def download(self, url, *, body=b"\xff\xd8\xffphoto", headers=None, status=200, max_bytes=100):
        settings = Settings(_env_file=None, home_assistant_url="https://ha.example:8123", home_assistant_token="test-token",
                            nutrition_image_max_bytes=max_bytes)
        service = NutritionService(settings, AsyncMock(), AsyncMock())
        requests = []
        def serve(request):
            requests.append(request)
            return httpx.Response(status, content=body, headers=headers)
        original = httpx.AsyncClient
        def client(**kwargs):
            return original(transport=httpx.MockTransport(serve), **kwargs)
        with patch("lumen.services.nutrition.httpx.AsyncClient", side_effect=client):
            result = await service.download_image(ConnectorFile(url=url))
        return result, requests

    async def test_exact_origin_auth_and_magic_mime(self):
        (data, mime), requests = await self.download("https://ha.example:8123/local/a.jpg")
        self.assertEqual("image/jpeg", mime)
        self.assertEqual(b"\xff\xd8\xffphoto", data)
        self.assertEqual("Bearer test-token", requests[0].headers["Authorization"])

    async def test_foreign_prefix_credentials_ports_and_scheme_rejected(self):
        for url in ("https://ha.example:8123.evil/local/a.jpg", "https://ha.example.evil:8123/a.jpg",
                    "https://user:pass@ha.example:8123/a.jpg", "http://ha.example:8123/a.jpg", "https://ha.example/a.jpg", "file:///photo.jpg"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await self.download(url)

    async def test_redirects_and_oversized_or_invalid_content_rejected(self):
        for kwargs in ({"status": 302, "headers": {"location": "https://evil.example/a.jpg"}},
                       {"headers": {"content-length": "1000"}}, {"body": b"x" * 101},
                       {"body": b"<html>not an image</html>"}, {"body": b""}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await self.download("https://ha.example:8123/local/a.jpg", **kwargs)

    async def test_png_and_webp_supported(self):
        for body, mime in ((b"\x89PNG\r\n\x1a\npayload", "image/png"), (b"RIFFxxxxWEBPpayload", "image/webp")):
            (data, actual), _ = await self.download("https://ha.example:8123/a", body=body)
            self.assertEqual(mime, actual)


if __name__ == "__main__":
    unittest.main()
