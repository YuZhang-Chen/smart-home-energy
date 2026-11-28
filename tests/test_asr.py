"""Unit and HTTP contract tests for vLLM Whisper ASR integration."""

import asyncio
from dataclasses import replace
import importlib.util
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from smart_home_energy.asr import (
    AsrAuthError,
    AsrConfig,
    AsrConfigurationError,
    AsrInputError,
    AsrRateLimitError,
    AsrUpstreamError,
    MAX_AUDIO_BYTES,
    transcribe_audio,
)

WEB_AVAILABLE = importlib.util.find_spec("fastapi") is not None
if WEB_AVAILABLE:
    from fastapi.testclient import TestClient
    from smart_home_energy.api import create_app
    from smart_home_energy.config import HemsSettings
    from smart_home_energy.repository import Repository


class FakeAsyncOpenAI:
    def __init__(self, response_text: str = "把客廳燈調到 40% 暖光", side_effect=None):
        self.calls: list[dict] = []
        self._response_text = response_text
        self._side_effect = side_effect
        self.audio = SimpleNamespace(
            transcriptions=SimpleNamespace(create=self._create),
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._side_effect is not None:
            raise self._side_effect
        return SimpleNamespace(text=self._response_text)


class AsrConfigTests(unittest.TestCase):
    def test_from_env_reuses_vllm_url_and_key_with_default_asr_model(self):
        env = {
            "VLLM_BASE_URL": "https://example.edu.tw/aihub/v1",
            "VLLM_API_KEY": "sk-test-key",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = AsrConfig.from_env(load_env=False)
        self.assertEqual(cfg.base_url, "https://example.edu.tw/aihub/v1/")
        self.assertEqual(cfg.api_key, "sk-test-key")
        self.assertEqual(cfg.model, "Asr")
        self.assertIsNone(cfg.language)

    def test_from_env_respects_asr_overrides(self):
        env = {
            "VLLM_BASE_URL": "https://example.edu.tw/aihub/v1",
            "VLLM_API_KEY": "sk-main",
            "VLLM_ASR_MODEL": "CustomAsr",
            "VLLM_ASR_LANGUAGE": "zh",
            "VLLM_ASR_TIMEOUT": "15",
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = AsrConfig.from_env(load_env=False)
        self.assertEqual(cfg.model, "CustomAsr")
        self.assertEqual(cfg.language, "zh")
        self.assertEqual(cfg.timeout, 15.0)

    def test_from_env_rejects_missing_or_invalid_url(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AsrConfigurationError):
                AsrConfig.from_env(load_env=False)

        with patch.dict(os.environ, {"VLLM_BASE_URL": "https://example.edu.tw/aihub"}, clear=True):
            with self.assertRaises(AsrConfigurationError):
                AsrConfig.from_env(load_env=False)


class TranscribeAudioTests(unittest.TestCase):
    def setUp(self):
        self.config = AsrConfig(
            base_url="https://example.edu.tw/aihub/v1/",
            api_key="sk-test",
            model="Asr",
        )

    def test_transcribe_audio_sends_model_and_file_without_language_by_default(self):
        fake = FakeAsyncOpenAI("  幫我打開電視並切換到 HDMI 2  ")
        result = asyncio.run(
            transcribe_audio(
                b"fake-webm-bytes",
                content_type="audio/webm;codecs=opus",
                filename="voice.webm",
                config=self.config,
                client_factory=lambda _cfg: fake,
            )
        )
        self.assertEqual(result, "幫我打開電視並切換到 HDMI 2")
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertEqual(call["model"], "Asr")
        self.assertNotIn("language", call)
        self.assertEqual(call["file"], ("voice.webm", b"fake-webm-bytes", "audio/webm"))

    def test_transcribe_audio_aligns_filename_extension_with_wav_mime(self):
        fake = FakeAsyncOpenAI("打開冷氣")
        result = asyncio.run(
            transcribe_audio(
                b"RIFF\x00\x00\x00\x00WAVE",
                content_type="audio/wav",
                filename="recording.webm",
                config=self.config,
                client_factory=lambda _cfg: fake,
            )
        )
        self.assertEqual(result, "打開冷氣")
        self.assertEqual(fake.calls[0]["file"][0], "recording.wav")
        self.assertEqual(fake.calls[0]["file"][2], "audio/wav")

    def test_transcribe_audio_rejects_empty_oversized_and_bad_mime(self):
        with self.assertRaises(AsrInputError) as empty_ctx:
            asyncio.run(transcribe_audio(b"", config=self.config))
        self.assertEqual(empty_ctx.exception.status_code, 400)

        with self.assertRaises(AsrInputError) as big_ctx:
            asyncio.run(transcribe_audio(b"a" * (MAX_AUDIO_BYTES + 1), config=self.config))
        self.assertEqual(big_ctx.exception.status_code, 413)

        with self.assertRaises(AsrInputError):
            asyncio.run(transcribe_audio(b"hello", content_type="text/plain", config=self.config))


@unittest.skipUnless(WEB_AVAILABLE, "Install .[web] for HTTP tests")
class AsrApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(TemporaryDirectory()))
        self.repo = Repository(self.directory / "control.db")
        self.hems = replace(
            HemsSettings.from_env(),
            database=self.repo.path,
            device_database=str(self.directory / "washer.db"),
        )
        self.client = self.enterContext(
            TestClient(create_app(self.repo, self.hems, self.directory / "no-build"))
        )

    def test_asr_transcribe_endpoint_success(self):
        with patch("smart_home_energy.asr.transcribe_audio", new=AsyncMock(return_value="打開客廳窗簾")) as mocked:
            response = self.client.post(
                "/api/asr/transcribe",
                content=b"webm-audio-data",
                headers={"Content-Type": "audio/webm", "X-Audio-Filename": "clip.webm"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"text": "打開客廳窗簾"})
        mocked.assert_awaited_once_with(b"webm-audio-data", content_type="audio/webm", filename="clip.webm")

    def test_asr_transcribe_maps_errors_to_http_status_codes(self):
        cases = [
            (AsrConfigurationError("未設定"), 503),
            (AsrInputError("空白", status_code=400), 400),
            (AsrInputError("過大", status_code=413), 413),
            (AsrAuthError("認證失敗"), 401),
            (AsrRateLimitError("限流"), 429),
            (AsrUpstreamError("上游錯誤"), 502),
        ]
        for exc, expected_status in cases:
            with patch("smart_home_energy.asr.transcribe_audio", new=AsyncMock(side_effect=exc)):
                response = self.client.post(
                    "/api/asr/transcribe",
                    content=b"audio",
                    headers={"Content-Type": "audio/webm"},
                )
            self.assertEqual(response.status_code, expected_status)
