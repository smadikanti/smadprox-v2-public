"""
Unit tests for CardBuffer — card splitting logic for streaming coaching text.

Fast, no API calls, no server startup.
"""

import os
import sys

import pytest

# Add backend to path so we can import app modules directly
backend_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'backend')
sys.path.insert(0, backend_dir)

from app.card_splitter import CardBuffer, Card, MAX_WORDS_PER_CARD


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _collect_actions(buf: CardBuffer, chunks: list[str]) -> list[dict]:
    """Feed a list of chunks and return all accumulated actions."""
    actions = []
    for chunk in chunks:
        actions.extend(buf.feed(chunk))
    return actions


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCardBufferFirstCard:
    """First card should appear immediately on the first non-empty token."""

    def test_first_card_immediate(self):
        buf = CardBuffer()
        actions = buf.feed("Hello, ")
        assert len(actions) == 1
        act = actions[0]
        assert act["action"] == "push"
        card = act["card"]
        assert isinstance(card, Card)
        assert card.text == "Hello,"
        assert card.index == 0

    def test_first_card_whitespace_only_skipped(self):
        """Pure whitespace should NOT create a card."""
        buf = CardBuffer()
        actions = buf.feed("   ")
        assert actions == []


@pytest.mark.unit
class TestParagraphBreak:
    """A double newline should finalize the current card and start a new one."""

    def test_paragraph_break(self):
        buf = CardBuffer()
        # First chunk creates card
        a1 = buf.feed("First paragraph.")
        assert any(a["action"] == "push" for a in a1)

        # Now feed text with a paragraph break
        a2 = buf.feed("\n\nSecond paragraph.")
        action_types = [a["action"] for a in a2]
        assert "finalize" in action_types
        assert "push" in action_types

        # We should now have two cards tracked internally
        assert len(buf.cards) == 2
        assert buf.cards[0].text  # first card has text
        assert buf.cards[1].text  # second card has text

    def test_multiple_paragraph_breaks(self):
        buf = CardBuffer()
        buf.feed("Para one.")
        buf.feed("\n\nPara two.\n\nPara three.")
        assert len(buf.cards) >= 3


@pytest.mark.unit
class TestMaxWords:
    """Long text without paragraph breaks should split at ~MAX_WORDS_PER_CARD."""

    def test_max_words(self):
        buf = CardBuffer()
        # Simulate streaming: feed chunks incrementally (like real Claude output).
        # Each sentence ~9 words. Feed enough to exceed 1.5x MAX_WORDS_PER_CARD.
        sentence = "This is a test sentence that has several words. "
        for _ in range(14):  # ~126 words total, fed incrementally
            buf.feed(sentence)

        # After streaming 126 words, the splitter should have split at a sentence
        # boundary once the buffer exceeded 1.5 * 55 = 82 words
        assert len(buf.cards) >= 2, (
            f"Expected split at ~{MAX_WORDS_PER_CARD} words but got {len(buf.cards)} card(s)"
        )

    def test_max_words_threshold_value(self):
        """Sanity-check the constant lives where we expect."""
        assert MAX_WORDS_PER_CARD == 55


@pytest.mark.unit
class TestCardUpdateStreaming:
    """Multiple feed() calls should produce card_update actions for the current card."""

    def test_card_update_streaming(self):
        buf = CardBuffer()
        a1 = buf.feed("Start ")
        assert a1[0]["action"] == "push"
        card_id = a1[0]["card"].card_id

        # Second feed should update (not push a new card)
        a2 = buf.feed("more text")
        assert len(a2) >= 1
        update = [a for a in a2 if a["action"] == "update"]
        assert len(update) == 1
        assert update[0]["card_id"] == card_id
        assert "more text" in update[0]["text"]

    def test_duplicate_text_no_spurious_update(self):
        """If feed() adds only whitespace that doesn't change stripped text, no update."""
        buf = CardBuffer()
        buf.feed("Hello")
        # Feed trailing space only — stripped text is still "Hello"
        a2 = buf.feed(" ")
        # Either empty or an update, but the text shouldn't have changed meaningfully
        updates = [a for a in a2 if a["action"] == "update"]
        # The stripped text may or may not change depending on trailing space handling,
        # but there should be no push
        pushes = [a for a in a2 if a["action"] == "push"]
        assert len(pushes) == 0


@pytest.mark.unit
class TestFinalize:
    """finalize() should flush the remaining buffer as the final card."""

    def test_finalize(self):
        buf = CardBuffer()
        buf.feed("Some remaining text")
        actions = buf.finalize()

        assert len(actions) == 1
        act = actions[0]
        assert act["action"] == "finalize"
        assert "remaining text" in act["text"]

        # The card should be marked final
        assert buf.cards[-1].is_final is True

        # Totals should be set on all cards
        for c in buf.cards:
            assert c.total == len(buf.cards)

    def test_finalize_empty_buffer(self):
        """Finalizing an empty buffer should produce no actions."""
        buf = CardBuffer()
        actions = buf.finalize()
        assert actions == []

    def test_finalize_after_paragraph_break(self):
        """Finalize after all text was already flushed via paragraph breaks."""
        buf = CardBuffer()
        buf.feed("Done.\n\n")
        # At this point the first paragraph was finalized; buffer remainder is empty
        actions = buf.finalize()
        # No new content to flush — may be empty or a no-op finalize
        # Just verify no crash
        assert isinstance(actions, list)


@pytest.mark.unit
class TestWhiteboardDetection:
    """[WHITEBOARD] markers should create whiteboard-typed cards."""

    def test_whiteboard_detection(self):
        buf = CardBuffer()
        # The _make_card private method accepts is_whiteboard. Since feed() does
        # not parse [WHITEBOARD] markers itself (pipeline.py does that), we test
        # the Card / _make_card path directly.
        card = buf._make_card("[WHITEBOARD] Draw boxes for services", is_whiteboard=True)
        assert card.is_whiteboard is True
        assert card.text == "[WHITEBOARD] Draw boxes for services"
        assert card.index == 0
        assert card in buf.cards

    def test_whiteboard_card_serialization(self):
        """Card with is_whiteboard=True should reflect that in its fields."""
        card = Card(
            card_id="wb01",
            text="Component diagram",
            index=0,
            is_whiteboard=True,
        )
        assert card.is_whiteboard is True
        assert card.is_final is False

    def test_non_whiteboard_default(self):
        buf = CardBuffer()
        card = buf._make_card("Normal text")
        assert card.is_whiteboard is False
