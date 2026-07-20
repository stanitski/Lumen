from __future__ import annotations

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lumen.connectors.ollama import OllamaConnector, OllamaInputPart, OllamaMessage


class OllamaConnectorRuntimeContextTest(unittest.TestCase):
    def test_chat_messages_are_prefixed_with_runtime_context(self) -> None:
        messages = [
            OllamaMessage(role="system", content="Existing system prompt."),
            OllamaMessage(role="user", content="What is today?"),
        ]

        enriched = OllamaConnector._with_runtime_context(messages)

        self.assertEqual(enriched[0].role, "system")
        self.assertIn("Runtime context:", enriched[0].content)
        self.assertIn("Current local date and time:", enriched[0].content)
        self.assertIn("Timezone:", enriched[0].content)
        self.assertEqual(enriched[1:], messages)

    def test_runtime_context_preserves_multimodal_messages(self) -> None:
        image_message = OllamaMessage(
            role="user",
            parts=[
                OllamaInputPart(type="text", text="Analyze this."),
                OllamaInputPart(type="image", data=b"image-bytes", mime_type="image/png"),
            ],
        )

        enriched = OllamaConnector._with_runtime_context([image_message])

        self.assertEqual(enriched[0].role, "system")
        self.assertEqual(enriched[1].parts[1].data, b"image-bytes")


if __name__ == "__main__":
    unittest.main()
