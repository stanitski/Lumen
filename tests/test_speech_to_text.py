from __future__ import annotations

import unittest

from lumen.services.speech_to_text import SpeechToTextService


class SpeechAttachmentTests(unittest.TestCase):
    def test_detects_audio_and_video_attachments(self) -> None:
        self.assertTrue(SpeechToTextService.is_speech_attachment(file_name="voice.ogg", mime_type="audio/ogg"))
        self.assertTrue(SpeechToTextService.is_speech_attachment(file_name="clip.mp4", mime_type="video/mp4"))

    def test_rejects_non_speech_attachment(self) -> None:
        self.assertFalse(SpeechToTextService.is_speech_attachment(file_name="notes.pdf", mime_type="application/pdf"))


if __name__ == "__main__":
    unittest.main()
