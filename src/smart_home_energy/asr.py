"""vLLM OpenAI-compatible ASR (Whisper) client and configuration."""

from dataclasses import dataclass
import math
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

from smart_home_energy.config import ENV_FILE

DEFAULT_ASR_MODEL = "Asr"
DEFAULT_ASR_TIMEOUT = 30.0
MAX_AUDIO_BYTES = 10 * 1024 * 1024  # 10 MB

ALLOWED_AUDIO_MIME_TYPES = frozenset({
    "audio/webm",
    "audio/wav",
    "audio/x-wav",
    "audio/wave",
    "audio/mp4",
    "audio/m4a",
    "audio/x-m4a",
    "audio/mpeg",
    "audio/mp3",
    "audio/ogg",
    "audio/flac",
    "audio/aac",
    "application/octet-stream",
})

MIME_TO_EXT = {
    "audio/webm": ".webm",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/wave": ".wav",
    "audio/mp4": ".mp4",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/aac": ".aac",
}


class AsrConfigurationError(RuntimeError):
    """Raised when vLLM ASR configuration is missing or invalid."""


class AsrInputError(ValueError):
    """Raised when the uploaded audio payload is empty, oversized, or unsupported."""

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


class AsrAuthError(RuntimeError):
    """Raised when the upstream vLLM gateway rejects credentials."""


class AsrRateLimitError(RuntimeError):
    """Raised when the upstream vLLM gateway rate-limits ASR requests."""


class AsrUpstreamError(RuntimeError):
    """Raised when the upstream vLLM gateway is unreachable, times out, or fails."""


@dataclass(frozen=True)
class AsrConfig:
    base_url: str
    api_key: str
    model: str = DEFAULT_ASR_MODEL
    language: str | None = None
    timeout: float = DEFAULT_ASR_TIMEOUT

    @classmethod
    def from_env(cls, *, load_env: bool = True) -> "AsrConfig":
        if load_env:
            load_dotenv(ENV_FILE, override=False)

        raw_url = (os.getenv("VLLM_ASR_BASE_URL") or os.getenv("VLLM_BASE_URL") or "").strip()
        raw_key = (os.getenv("VLLM_ASR_API_KEY") or os.getenv("VLLM_API_KEY") or "").strip()
        raw_model = (os.getenv("VLLM_ASR_MODEL") or DEFAULT_ASR_MODEL).strip()
        raw_language = (os.getenv("VLLM_ASR_LANGUAGE") or "").strip() or None
        raw_timeout = (os.getenv("VLLM_ASR_TIMEOUT") or str(DEFAULT_ASR_TIMEOUT)).strip()

        if not raw_url:
            raise AsrConfigurationError("尚未設定 VLLM_BASE_URL，無法使用語音辨識服務")
        if not raw_model:
            raise AsrConfigurationError("VLLM_ASR_MODEL 不可為空白")

        parsed = urlparse(raw_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise AsrConfigurationError("vLLM ASR URL 格式不合法")
        if not raw_url.rstrip("/").endswith("/v1"):
            raise AsrConfigurationError("VLLM_BASE_URL 必須包含 /v1")

        try:
            timeout = float(raw_timeout)
        except ValueError as error:
            raise AsrConfigurationError("VLLM_ASR_TIMEOUT 必須為有效數字") from error
        if not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise AsrConfigurationError("VLLM_ASR_TIMEOUT 必須介於 0 到 120 秒之間")

        return cls(
            base_url=raw_url.rstrip("/") + "/",
            api_key=raw_key or "not-required",
            model=raw_model,
            language=raw_language,
            timeout=timeout,
        )


def is_asr_configured(*, load_env: bool = True) -> bool:
    try:
        AsrConfig.from_env(load_env=load_env)
        return True
    except AsrConfigurationError:
        return False


def _sanitize_audio_metadata(content_type: str, filename: str | None) -> tuple[str, str]:
    base_mime = (content_type or "").split(";", 1)[0].strip().lower() or "audio/webm"
    if base_mime not in ALLOWED_AUDIO_MIME_TYPES:
        raise AsrInputError(f"不支援的音訊格式：{base_mime}")

    effective_mime = "audio/webm" if base_mime == "application/octet-stream" else base_mime
    default_ext = MIME_TO_EXT.get(effective_mime, ".webm")

    raw_name = Path((filename or "").strip()).name
    if not raw_name or raw_name in {".", ".."}:
        safe_name = f"recording{default_ext}"
    else:
        stem = Path(raw_name).stem or "recording"
        safe_name = f"{stem}{default_ext}"

    return effective_mime, safe_name


async def transcribe_audio(
    audio_bytes: bytes,
    *,
    content_type: str = "audio/webm",
    filename: str | None = None,
    config: AsrConfig | None = None,
    client_factory=None,
) -> str:
    if not audio_bytes:
        raise AsrInputError("音訊內容不可為空")
    if len(audio_bytes) > MAX_AUDIO_BYTES:
        raise AsrInputError("音訊檔案超過 10 MB 上限，請錄製較短的語音", status_code=413)

    effective_mime, safe_name = _sanitize_audio_metadata(content_type, filename)
    cfg = config or AsrConfig.from_env()

    try:
        from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI
    except ImportError:
        raise AsrConfigurationError("尚未安裝 OpenAI SDK，請執行 pip install -e .") from None

    kwargs: dict = {
        "model": cfg.model,
        "file": (safe_name, audio_bytes, effective_mime),
    }
    if cfg.language:
        kwargs["language"] = cfg.language

    factory = client_factory or (
        lambda c: AsyncOpenAI(
            base_url=c.base_url,
            api_key=c.api_key,
            max_retries=0,
            timeout=c.timeout,
        )
    )

    try:
        async with factory(cfg) as client:
            response = await client.audio.transcriptions.create(**kwargs)
    except (APIConnectionError, APITimeoutError):
        raise AsrUpstreamError("ASR 服務連線失敗或逾時") from None
    except APIStatusError as error:
        # Never expose upstream headers or raw error bodies that may include credentials.
        if error.status_code in {401, 403}:
            raise AsrAuthError(f"ASR HTTP {error.status_code}：認證失敗，請檢查 VLLM_API_KEY") from None
        if error.status_code == 429:
            raise AsrRateLimitError("ASR HTTP 429：請求過於頻繁，請稍後再試") from None
        if error.status_code in {400, 413, 415, 422}:
            raise AsrInputError(f"ASR 無法處理音訊（HTTP {error.status_code}）", status_code=400) from None
        raise AsrUpstreamError(f"ASR 上游服務錯誤（HTTP {error.status_code}）") from None

    text = response if isinstance(response, str) else getattr(response, "text", "")
    return (text or "").strip()
