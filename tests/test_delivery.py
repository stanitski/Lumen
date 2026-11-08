import unittest
from unittest.mock import AsyncMock

from lumen.config import Settings
from lumen.services.delivery import DeliveryService


class TelegramTextDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = DeliveryService(Settings())
        self.service.post_home_assistant_callback = AsyncMock()

    async def send(self, answer, *, voice=False, channel="telegram"):
        await self.service.deliver_chat_answer(
            request_id="request", user_id="user", answer=answer, voice=voice,
            metadata={"target_channel": channel, "target_chat_id": "chat"},
        )
        return [call.args[0] for call in self.service.post_home_assistant_callback.await_args_list]

    async def test_long_transcript_preserves_words_and_route(self):
        answer = "\n\n".join(f"21:{i:02}: " + "повідомлення " * 45 for i in range(17))
        parts = await self.send(answer)
        self.assertGreater(len(parts), 1)
        self.assertEqual(answer.split(), " ".join(p.answer for p in parts).split())
        for i, part in enumerate(parts, 1):
            self.assertLessEqual(len(part.answer.encode("utf-16-le")) // 2, 4096)
            self.assertEqual(part.request_id, "request")
            self.assertEqual(part.metadata["target_chat_id"], "chat")
            self.assertEqual(part.metadata["part_index"], i)
            self.assertEqual(part.metadata["part_count"], len(parts))
            self.assertFalse(part.voice)
            self.assertEqual(part.voice_chunks, [])

    async def test_emoji_and_unbroken_word_respect_limit(self):
        answer = "😀" * 3000
        parts = await self.send(answer)
        self.assertGreater(len(parts), 1)
        self.assertEqual(answer, "".join(p.answer for p in parts))
        self.assertTrue(all(len(p.answer.encode("utf-16-le")) // 2 <= 4096 for p in parts))

    async def test_at_limit_is_one_unchanged_callback(self):
        answer = "я" * 4096
        parts = await self.send(answer)
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].answer, answer)
        self.assertNotIn("part_index", parts[0].metadata)

    async def test_other_channel_is_unchanged(self):
        answer = "я" * 6000
        parts = await self.send(answer, channel="home_assistant")
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0].answer, answer)

    async def test_voice_keeps_existing_chunks(self):
        parts = await self.send("слово " * 1000, voice=True)
        self.assertEqual(len(parts), 1)
        self.assertGreater(len(parts[0].voice_chunks), 1)
        self.assertNotIn("part_index", parts[0].metadata)

    async def test_failure_stops_remaining_parts(self):
        self.service.post_home_assistant_callback.side_effect = [None, RuntimeError("failed")]
        with self.assertRaisesRegex(RuntimeError, "failed"):
            await self.send("слово " * 2000)
        self.assertEqual(self.service.post_home_assistant_callback.await_count, 2)

    async def test_knowledge_text_uses_same_split(self):
        await self.service.deliver_knowledge_message(
            request_id="request", user_id="user", answer="я" * 6000,
            metadata={"target_channel": "telegram"},
        )
        parts = [c.args[0] for c in self.service.post_home_assistant_callback.await_args_list]
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(p.kind == "knowledge" for p in parts))


if __name__ == "__main__":
    unittest.main()
