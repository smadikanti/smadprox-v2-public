"""
ElevenLabs Streaming Text-to-Speech for HumanProx mode.

Uses the ElevenLabs v1 streaming API to convert AI-generated text
into audio chunks that are relayed to the dashboard in real-time.
"""

import base64
import logging
from typing import AsyncGenerator

import httpx

from app.config import settings

logger = logging.getLogger("nohuman.tts")


def elevenlabs_available() -> bool:
    return bool(settings.ELEVENLABS_API_KEY and settings.ELEVENLABS_VOICE_ID)


TTS_SAMPLE_RATE = 24000  # pcm_24000 = 24kHz, 16-bit signed LE

async def stream_tts(
    text: str,
    voice_id: str | None = None,
    model_id: str = "eleven_turbo_v2_5",
    output_format: str = "pcm_24000",
) -> AsyncGenerator[bytes, None]:
    """
    Stream TTS audio from ElevenLabs as raw PCM (24kHz, 16-bit signed LE).

    Yields raw PCM bytes that can be played directly on the client
    without MP3 decoding — eliminates choppy playback from partial frames.
    """
    if not elevenlabs_available():
        logger.warning("[TTS] ElevenLabs not configured, skipping")
        return

    vid = voice_id or settings.ELEVENLABS_VOICE_ID
    url = (
        f"https://api.elevenlabs.io/v1/text-to-speech/{vid}/stream"
        f"?output_format={output_format}"
    )

    headers = {
        "xi-api-key": settings.ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
    }

    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": 0.5,
            "similarity_boost": 0.75,
            "style": 0.0,
            "use_speaker_boost": True,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            async with client.stream(
                "POST", url, json=payload, headers=headers
            ) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    logger.error(
                        f"[TTS] ElevenLabs API error {resp.status_code}: "
                        f"{body[:200]}"
                    )
                    return

                async for chunk in resp.aiter_bytes(chunk_size=8192):
                    if chunk:
                        yield chunk

    except httpx.TimeoutException:
        logger.error("[TTS] ElevenLabs request timed out")
    except Exception as e:
        logger.error(f"[TTS] ElevenLabs streaming error: {e}")


async def tts_to_base64_chunks(
    text: str,
    voice_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """
    Convenience wrapper: yields base64-encoded audio chunks
    ready to embed in WebSocket JSON messages.
    """
    async for raw_chunk in stream_tts(text, voice_id=voice_id):
        yield base64.b64encode(raw_chunk).decode("ascii")
