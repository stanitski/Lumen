"""Speech-to-text for connector audio and video messages."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any

import httpx

from lumen.config import Settings


class SpeechToTextError(RuntimeError):
    """Raised when a speech attachment cannot be transcribed."""


class SpeechToTextService:
    def __init__(self, settings: Settings, *, logger: logging.Logger | None = None) -> None:
        self._settings = settings
        self._logger = logger or logging.getLogger("lumen")
        self._model: Any | None = None

    @staticmethod
    def is_speech_attachment(*, file_name: str, mime_type: str) -> bool:
        suffix = Path(file_name).suffix.lower()
        mime = mime_type.strip().lower()
        return mime.startswith(("audio/", "video/")) or mime in {"application/ogg", "video/ogg"} or suffix in {
            ".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".flac", ".aac", ".webm", ".mp4", ".mov", ".mkv", ".m4v", ".avi"
        }

    async def transcribe_url(self, *, file_url: str, file_name: str, mime_type: str) -> str:
        if not file_url.strip():
            raise SpeechToTextError("Speech attachment download URL is missing.")
        try:
            url = httpx.URL(file_url)
            trusted = httpx.URL(self._settings.home_assistant_url)
            if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
                raise SpeechToTextError("Invalid speech attachment URL.")
            headers: dict[str, str] = {}
            # Compare parsed origins: a hostname prefix can belong to another server.
            if (url.scheme, url.host, url.port) == (trusted.scheme, trusted.host, trusted.port) and self._settings.home_assistant_token.strip():
                headers["Authorization"] = f"Bearer {self._settings.home_assistant_token.strip()}"
            async with httpx.AsyncClient(timeout=self._settings.ollama_timeout_seconds, follow_redirects=False) as client:
                response = await client.get(file_url, headers=headers)
            response.raise_for_status()
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            # Do not expose an attachment URL or credentials through endpoint logs.
            raise SpeechToTextError(f"Speech attachment download failed ({type(exc).__name__}).") from exc
        return await self.transcribe_bytes(raw=response.content, file_name=file_name, mime_type=mime_type)

    async def transcribe_bytes(self, *, raw: bytes, file_name: str, mime_type: str) -> str:
        if not raw:
            raise SpeechToTextError("Speech attachment is empty.")
        path = await self._prepare_audio(raw=raw, file_name=file_name, mime_type=mime_type)
        try:
            return await asyncio.to_thread(self._transcribe, path)
        finally:
            self._cleanup(path.parent)

    async def _prepare_audio(self, *, raw: bytes, file_name: str, mime_type: str) -> Path:
        directory = Path(tempfile.mkdtemp(prefix="lumen-stt-"))
        suffix = Path(file_name).suffix.lower() or self._suffix_for_mime(mime_type)
        source = directory / f"input{suffix}"
        target = directory / "audio.wav"
        source.write_bytes(raw)
        try:
            process = await asyncio.create_subprocess_exec(
                self._settings.ffmpeg_path.strip() or "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-ac", "1", "-ar", "16000", str(target),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            self._logger.warning("ffmpeg was not found; passing the original attachment to faster-whisper")
            return source
        _, stderr = await process.communicate()
        if process.returncode != 0 or not target.exists():
            raise SpeechToTextError(f"ffmpeg failed: {stderr.decode('utf-8', errors='replace').strip() or process.returncode}")
        source.unlink(missing_ok=True)
        return target

    def _transcribe(self, path: Path) -> str:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise SpeechToTextError("Speech transcription requires faster-whisper.") from exc
        if self._model is None:
            self._model = WhisperModel(self._settings.stt_model, device=self._settings.stt_device, compute_type=self._settings.stt_compute_type)
        segments, _ = self._model.transcribe(str(path), language=self._settings.stt_language.strip() or None)
        text = " ".join(str(segment.text).strip() for segment in segments if str(segment.text).strip()).strip()
        if not text:
            raise SpeechToTextError("Speech transcription returned empty text.")
        return text

    @staticmethod
    def _suffix_for_mime(mime_type: str) -> str:
        mime = mime_type.lower()
        if "webm" in mime:
            return ".webm"
        if "mp4" in mime or "m4a" in mime:
            return ".mp4"
        if "wav" in mime:
            return ".wav"
        return ".ogg"

    @staticmethod
    def _cleanup(directory: Path) -> None:
        for path in directory.glob("*"):
            path.unlink(missing_ok=True)
        directory.rmdir()
