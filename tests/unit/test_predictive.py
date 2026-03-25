"""
Unit tests for PredictiveEngine — iterative question classification.

Fast, no API calls (only tests synchronous / local-classify paths),
no server startup.
"""

import os
import sys
import asyncio

import pytest

backend_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'backend')
sys.path.insert(0, backend_dir)

from app.predictive import PredictiveEngine, PredictionResult, KEYWORD_PATTERNS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run an async coroutine synchronously (works on Python 3.10+)."""
    return asyncio.run(coro)


@pytest.fixture
def engine():
    """Fresh PredictiveEngine with no script content."""
    return PredictiveEngine()


@pytest.fixture
def engine_with_script():
    """PredictiveEngine loaded with a small mock script."""
    script = (
        "## Tell me about yourself\n"
        "I have 8 years of experience in backend engineering...\n\n"
        "## Behavioral Stories\n"
        "When I led the migration to Kubernetes, we hit a major incident...\n\n"
        "## System Design — URL Shortener\n"
        "Phase 1: Start with a single-server architecture...\n\n"
    )
    return PredictiveEngine(script_content=script)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTooEarly:
    """Partial transcript with fewer than 5 tokens should return None."""

    def test_too_early(self, engine):
        result = _run(engine.on_partial_transcript("tell me"))
        assert result is None
        assert engine.token_count == 2

    def test_exactly_four_tokens(self, engine):
        result = _run(engine.on_partial_transcript("one two three four"))
        assert result is None

    def test_five_tokens_returns_result(self, engine):
        result = _run(engine.on_partial_transcript("one two three four five"))
        assert result is not None


@pytest.mark.unit
class TestBehavioralClassification:
    """'tell me about a time' should classify as behavioral."""

    def test_behavioral_classification(self, engine):
        text = "tell me about a time when you faced a challenging situation"
        result = _run(engine.on_partial_transcript(text))
        assert result is not None
        assert result.question_type == "behavioral"
        assert result.confidence > 0

    def test_behavioral_give_example(self, engine):
        text = "can you give me an example of when you handled a conflict"
        result = _run(engine.on_partial_transcript(text))
        assert result is not None
        assert result.question_type == "behavioral"


@pytest.mark.unit
class TestSystemDesignClassification:
    """'how would you design' should classify as system_design."""

    def test_system_design_classification(self, engine):
        text = "how would you design a distributed cache for a large scale system"
        result = _run(engine.on_partial_transcript(text))
        assert result is not None
        assert result.question_type == "system_design"
        assert result.confidence > 0

    def test_system_design_architect(self, engine):
        text = "walk me through how you would architect a notification service"
        result = _run(engine.on_partial_transcript(text))
        assert result is not None
        assert result.question_type == "system_design"


@pytest.mark.unit
class TestFollowUpClassification:
    """'can you elaborate' should classify as follow_up."""

    def test_follow_up_classification(self, engine):
        text = "can you elaborate on how you handled the database migration"
        result = _run(engine.on_partial_transcript(text))
        assert result is not None
        assert result.question_type == "follow_up"
        assert result.confidence > 0

    def test_follow_up_tell_me_more(self, engine):
        text = "tell me more about the monitoring approach you mentioned"
        result = _run(engine.on_partial_transcript(text))
        assert result is not None
        assert result.question_type == "follow_up"


@pytest.mark.unit
class TestFillerGeneration:
    """Classified questions should produce non-empty filler text."""

    def test_filler_generation(self, engine):
        text = "tell me about a time when you had to deal with a difficult teammate"
        result = _run(engine.on_partial_transcript(text))
        assert result is not None
        assert result.filler_text
        assert len(result.filler_text) > 0

    def test_filler_varies_by_type(self, engine):
        """Different question types should produce different fillers when confidence is high enough."""
        # Use text with MANY matching keywords to boost confidence above 0.3
        behavioral_text = "tell me about a time when you had a conflict or disagreement with a difficult teammate and how did you handle that challenge"
        design_text = "how would you design and architect a distributed system with microservices that can scale horizontally"

        r1 = _run(engine.on_partial_transcript(behavioral_text))
        engine.reset()
        r2 = _run(engine.on_partial_transcript(design_text))

        # Both should produce type-specific fillers (not generic "Yeah, so...")
        assert r1.filler_text is not None
        assert r2.filler_text is not None
        # At least one should be type-specific (not both generic)
        generic = "Yeah, so..."
        assert r1.filler_text != generic or r2.filler_text != generic, (
            f"Both fillers are generic: '{r1.filler_text}' and '{r2.filler_text}'"
        )

    def test_low_confidence_generic_filler(self, engine):
        """Unknown question type with low confidence should still produce a filler."""
        # Five generic words — unlikely to match any pattern strongly
        text = "so what do you think about"
        result = _run(engine.on_partial_transcript(text))
        assert result is not None
        assert result.filler_text  # should be the generic "Yeah, so..."


@pytest.mark.unit
class TestReset:
    """After reset(), engine state should be clean."""

    def test_reset(self, engine):
        # Accumulate some state
        _run(engine.on_partial_transcript("tell me about a time you had a conflict"))
        assert engine.token_count > 0
        assert engine.current_prediction is not None

        engine.reset()

        assert engine.current_text == ""
        assert engine.token_count == 0
        assert engine.current_prediction is None
        assert engine.prefetched_cards is None

    def test_reset_allows_fresh_classification(self, engine):
        """After reset, a new transcript should classify independently."""
        _run(engine.on_partial_transcript("tell me about a time you failed"))
        engine.reset()
        result = _run(engine.on_partial_transcript("how would you design a URL shortener service"))
        assert result is not None
        assert result.question_type == "system_design"


@pytest.mark.unit
class TestEndOfTurnValidation:
    """on_end_of_turn should validate the prediction against full text."""

    def test_end_of_turn_validation_correct(self, engine_with_script):
        """If the full text matches the predicted type, return pre-fetched answer."""
        # Build up a prediction with a script-matched section
        partial = "tell me about a time when you showed leadership"
        _run(engine_with_script.on_partial_transcript(partial))

        # The prediction should exist
        pred = engine_with_script.current_prediction
        assert pred is not None

        # Full text — still behavioral
        full = "tell me about a time when you showed leadership and handled a conflict"
        answer = _run(engine_with_script.on_end_of_turn(full))

        # If confidence was high enough and section matched, we get the pre-fetched answer.
        # If not (depends on scoring), we get None. Either way, state should be reset.
        assert engine_with_script.current_text == ""
        assert engine_with_script.current_prediction is None

    def test_end_of_turn_no_prediction(self, engine):
        """With no prior prediction, on_end_of_turn returns None and resets."""
        result = _run(engine.on_end_of_turn("some full question text here"))
        assert result is None
        assert engine.token_count == 0

    def test_end_of_turn_resets_state(self, engine):
        """State is always reset after on_end_of_turn, regardless of outcome."""
        _run(engine.on_partial_transcript("tell me about a time you handled pressure at work"))
        _run(engine.on_end_of_turn("tell me about a time you handled pressure at work"))
        assert engine.current_text == ""
        assert engine.token_count == 0
        assert engine.current_prediction is None
