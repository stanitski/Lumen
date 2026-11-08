from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

import httpx

from lumen.config import Settings
from lumen.services.speech_to_text import SpeechToTextError, SpeechToTextService


class SpeechAttachmentTests(unittest.TestCase):
    def test_detects_audio_and_video_attachments(self) -> None:
        self.assertTrue(SpeechToTextService.is_speech_attachment(file_name="voice.ogg", mime_type="audio/ogg"))
        self.assertTrue(SpeechToTextService.is_speech_attachment(file_name="clip.mp4", mime_type="video/mp4"))

    def test_rejects_non_speech_attachment(self) -> None:
        self.assertFalse(SpeechToTextService.is_speech_attachment(file_name="notes.pdf", mime_type="application/pdf"))


class SpeechDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def download(self, url, status=200):
        service = SpeechToTextService(Settings(_env_file=None, home_assistant_url="https://ha.example", home_assistant_token="test-token"))
        service.transcribe_bytes = AsyncMock(return_value="speech")
        requests = []
        def serve(request):
            requests.append(request)
            return httpx.Response(status, content=b"audio", headers={"location": "https://other.example/file"})
        original = httpx.AsyncClient
        with patch("lumen.services.speech_to_text.httpx.AsyncClient", side_effect=lambda **kw: original(transport=httpx.MockTransport(serve), **kw)):
            await service.transcribe_url(file_url=url, file_name="voice.ogg", mime_type="audio/ogg")
        return requests

    async def test_token_only_sent_to_exact_origin(self):
        for url, expected in (("https://ha.example:443/file", True), ("https://ha.example.evil/file", False), ("https://ha.example:444/file", False), ("http://ha.example/file", False)):
            with self.subTest(url=url):
                requests = await self.download(url)
                self.assertEqual(expected, "authorization" in requests[0].headers)

    async def test_http_errors_and_redirects_use_sanitized_service_error(self):
        for status in (302, 404, 500):
            with self.subTest(status=status), self.assertRaises(SpeechToTextError) as error:
                await self.download("https://ha.example/private?secret=value", status)
            self.assertNotIn("secret", str(error.exception))

    async def test_credentials_in_url_rejected(self):
        with self.assertRaises(SpeechToTextError):
            await self.download("https://user:password@ha.example/file")


if __name__ == "__main__":
    unittest.main()
