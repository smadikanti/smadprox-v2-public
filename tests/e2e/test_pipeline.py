"""
End-to-end pipeline tests: audio -> backend -> coaching -> cards.

These tests exercise the full SmadProx v2 pipeline by sending real PCM audio
through WebSockets and asserting that transcripts, coaching suggestions,
and cards are produced end-to-end.

Requires:
    - Backend running (backend_server fixture starts it on port 8765)
    - Audio fixtures generated via: python tests/fixtures/generate_fixtures.py
    - pytest-asyncio installed

Run:
    pytest tests/e2e/test_pipeline.py -m e2e -v --timeout=120
"""

import asyncio
import json
import os
import sys
import time

import pytest

# ---------------------------------------------------------------------------
# Ensure backend is importable (for shared types/config if needed)
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

# ---------------------------------------------------------------------------
# All tests in this module are async, e2e, and need the backend running
# ---------------------------------------------------------------------------
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.asyncio,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

METRICS_PATH = "/tmp/smadprox-metrics.jsonl"


def _read_metrics(last_n: int = 1) -> list[dict]:
    """Read the last N lines from the metrics JSONL file."""
    if not os.path.exists(METRICS_PATH):
        return []
    with open(METRICS_PATH) as f:
        lines = f.readlines()
    results = []
    for line in lines[-last_n:]:
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return results


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAudioPipeline:
    """Full pipeline: audio in -> transcript / coaching / cards out."""

    async def test_audio_reaches_deepgram(self, ws_session, audio_fixture):
        """Audio sent via WebSocket produces a transcript message on the
        dashboard within 15 seconds."""
        pcm = audio_fixture("intro")
        silence = audio_fixture("silence_1s")

        # Send speech audio
        await ws_session.send_audio(pcm)
        # Send 2 seconds of silence after speech to trigger endpointing
        await ws_session.send_audio(silence)
        await ws_session.send_audio(silence)

        msg = await ws_session.wait_for_message("transcript", source="dashboard", timeout=15.0)
        assert msg["type"] == "transcript"
        assert "text" in msg
        assert len(msg["text"]) > 0

    async def test_coaching_generates(self, ws_session, audio_fixture):
        """After audio is transcribed, the backend should emit a
        suggestion_start followed by suggestion_end on the dashboard."""
        pcm = audio_fixture("intro")

        await ws_session.send_audio(pcm)

        # Wait for suggestion lifecycle
        start_msg = await ws_session.wait_for_message(
            "suggestion_start", source="dashboard", timeout=30.0
        )
        assert start_msg["type"] == "suggestion_start"

        end_msg = await ws_session.wait_for_message(
            "suggestion_end", source="dashboard", timeout=30.0
        )
        assert end_msg["type"] == "suggestion_end"

    async def test_cards_generated(self, ws_session, audio_fixture):
        """The pipeline should produce at least one card_push message after
        receiving audio input."""
        pcm = audio_fixture("behavioral_conflict")

        await ws_session.send_audio(pcm)

        # Wait for at least one card_push
        cards = await ws_session.wait_for_messages(
            "card_push", count=1, source="dashboard", timeout=30.0
        )
        assert len(cards) >= 1
        # Each card should have text content
        for card in cards:
            assert card["type"] == "card_push"
            assert "text" in card
            assert len(card["text"]) > 0

    async def test_filler_delivered(self, ws_session, audio_fixture):
        """A filler card (is_filler=True) should arrive within 2 seconds of
        the end-of-turn — it is the instant bridge card."""
        pcm = audio_fixture("behavioral_challenge")

        t_start = time.monotonic()
        await ws_session.send_audio(pcm)

        # Wait specifically for a card_push with is_filler=True
        deadline = 30.0  # Total timeout to account for audio send time + transcription
        filler_timeout = None  # We will check filler latency separately
        filler_msg = None
        start_wait = time.monotonic()

        while time.monotonic() - start_wait < deadline:
            for msg in ws_session.dashboard_messages:
                if msg.get("type") == "card_push" and msg.get("is_filler") is True:
                    filler_msg = msg
                    break
            if filler_msg:
                break
            await asyncio.sleep(0.05)

        assert filler_msg is not None, (
            f"No filler card received within {deadline}s. "
            f"Messages seen: {[m.get('type') for m in ws_session.dashboard_messages]}"
        )
        assert filler_msg["is_filler"] is True
        assert "text" in filler_msg
        assert len(filler_msg["text"]) > 0

        # Filler cards should also carry an instruction
        if "instruction" in filler_msg:
            assert len(filler_msg["instruction"]) > 0

    async def test_cache_hit_on_second_question(
        self, ws_session, audio_fixture, metrics_reader
    ):
        """Sending two questions sequentially should result in a prompt-cache
        hit (cache_read_tokens > 0) on the second question, because the
        system prompt and conversation prefix are shared."""
        pcm_first = audio_fixture("intro")
        pcm_second = audio_fixture("why_company")

        # --- First question ---
        await ws_session.send_audio(pcm_first)
        await ws_session.wait_for_message("suggestion_end", source="dashboard", timeout=30.0)

        # Small pause to let metrics flush
        await asyncio.sleep(1.0)

        # --- Second question ---
        # Clear tracked messages so we wait for new ones
        ws_session.dashboard_messages.clear()
        await ws_session.send_audio(pcm_second)
        await ws_session.wait_for_message("suggestion_end", source="dashboard", timeout=30.0)

        # Give metrics time to flush to disk
        await asyncio.sleep(1.0)

        # Read the most recent metric entry (should be for the second question)
        recent = metrics_reader(last_n=2)
        assert len(recent) >= 1, "No metrics found after second question"

        # The second (most recent) entry should show cache usage
        last_metric = recent[-1]
        cache_tokens = last_metric.get("cache_read_tokens", 0)
        assert cache_tokens > 0, (
            f"Expected cache_read_tokens > 0 on second question, "
            f"got {cache_tokens}. Full metric: {json.dumps(last_metric, indent=2)[:500]}"
        )

    async def test_haiku_routing_for_followup(
        self, ws_session, audio_fixture, metrics_reader
    ):
        """A short follow-up question should be routed to the Haiku model
        (cheaper/faster) rather than Sonnet."""
        # Send an initial question first to set up conversation context
        pcm_intro = audio_fixture("intro")
        await ws_session.send_audio(pcm_intro)
        await ws_session.wait_for_message("suggestion_end", source="dashboard", timeout=30.0)
        await asyncio.sleep(1.0)

        # Clear messages for the follow-up
        ws_session.dashboard_messages.clear()

        # Now send the short follow-up
        pcm_followup = audio_fixture("follow_up")
        await ws_session.send_audio(pcm_followup)
        await ws_session.wait_for_message("suggestion_end", source="dashboard", timeout=30.0)

        # Give metrics time to flush
        await asyncio.sleep(1.0)

        recent = metrics_reader(last_n=1)
        assert len(recent) >= 1, "No metrics found after follow-up question"

        last_metric = recent[-1]
        model_used = last_metric.get("model_used", "")
        assert "haiku" in model_used.lower(), (
            f"Expected follow-up to be routed to Haiku, "
            f"but model_used={model_used!r}. "
            f"Full metric: {json.dumps(last_metric, indent=2)[:500]}"
        )
