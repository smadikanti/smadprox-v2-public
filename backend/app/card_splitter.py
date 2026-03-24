"""
Card Splitter — segments coaching answers into relay-ready cards.

Streaming text from the coaching engine is split on paragraph boundaries,
[WHITEBOARD]/[SAY] markers, and word-count thresholds. Cards are pushed
to the candidate overlay via /ws/overlay/{session_id}.
"""

import uuid
import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger("nohuman.cards")

MAX_WORDS_PER_CARD = 80
MIN_WORDS_TO_FLUSH = 15


@dataclass
class Card:
    card_id: str
    text: str
    index: int
    total: int = 0
    is_whiteboard: bool = False
    is_continuation: bool = False
    is_final: bool = False


@dataclass
class CardBuffer:
    """Accumulates streaming tokens and emits cards as paragraphs form."""

    cards: list[Card] = field(default_factory=list)
    _buffer: str = ""
    _is_continuation: bool = False

    def reset(self, is_continuation: bool = False):
        self.cards = []
        self._buffer = ""
        self._is_continuation = is_continuation

    def feed(self, chunk: str) -> list[Card]:
        """Feed a streaming chunk. Returns any newly completed cards."""
        self._buffer += chunk
        return self._try_flush()

    def finalize(self) -> list[Card]:
        """Flush remaining buffer as the final card."""
        new_cards = []
        text = self._buffer.strip()
        if text:
            new_cards.extend(self._split_final(text))
            self._buffer = ""
        for c in new_cards:
            c.is_final = True
        return new_cards

    def _try_flush(self) -> list[Card]:
        """Check if buffer contains complete paragraphs to emit."""
        new_cards = []

        while True:
            # Split on double-newline (paragraph break)
            parts = re.split(r'\n\s*\n', self._buffer, maxsplit=1)
            if len(parts) < 2:
                break

            paragraph = parts[0].strip()
            self._buffer = parts[1]

            if not paragraph:
                continue

            cards = self._paragraph_to_cards(paragraph)
            new_cards.extend(cards)

        # Also flush if buffer is getting long (single giant paragraph)
        buf_words = len(self._buffer.split())
        if buf_words > MAX_WORDS_PER_CARD * 1.5:
            break_point = self._find_sentence_break(self._buffer, MAX_WORDS_PER_CARD)
            if break_point > 0:
                fragment = self._buffer[:break_point].strip()
                self._buffer = self._buffer[break_point:].lstrip()
                if fragment:
                    new_cards.extend(self._paragraph_to_cards(fragment))

        return new_cards

    def _split_final(self, text: str) -> list[Card]:
        """Split final text that may contain [WHITEBOARD]/[SAY] markers."""
        cards = []
        wb_pattern = re.compile(r'\[WHITEBOARD\](.*?)(?=\[SAY\]|\Z)', re.DOTALL | re.IGNORECASE)
        say_pattern = re.compile(r'\[SAY\](.*?)(?=\[WHITEBOARD\]|\Z)', re.DOTALL | re.IGNORECASE)

        has_markers = bool(wb_pattern.search(text)) or bool(say_pattern.search(text))

        if has_markers:
            for m in wb_pattern.finditer(text):
                content = m.group(1).strip()
                if content:
                    cards.append(self._make_card(content, is_whiteboard=True))
            for m in say_pattern.finditer(text):
                content = m.group(1).strip()
                if content:
                    cards.extend(self._split_long_text(content))
        else:
            cards.extend(self._split_long_text(text))

        return cards

    def _paragraph_to_cards(self, text: str) -> list[Card]:
        """Convert a paragraph to one or more cards."""
        wb_match = re.match(r'\[WHITEBOARD\]\s*(.*)', text, re.DOTALL | re.IGNORECASE)
        if wb_match:
            content = wb_match.group(1).strip()
            if content:
                return [self._make_card(content, is_whiteboard=True)]
            return []

        say_match = re.match(r'\[SAY\]\s*(.*)', text, re.DOTALL | re.IGNORECASE)
        if say_match:
            text = say_match.group(1).strip()

        if not text:
            return []

        return self._split_long_text(text)

    def _split_long_text(self, text: str) -> list[Card]:
        """Split text exceeding MAX_WORDS_PER_CARD on sentence boundaries."""
        words = text.split()
        if len(words) <= MAX_WORDS_PER_CARD:
            return [self._make_card(text)]

        cards = []
        remaining = text
        while remaining:
            r_words = remaining.split()
            if len(r_words) <= MAX_WORDS_PER_CARD:
                cards.append(self._make_card(remaining.strip()))
                break

            bp = self._find_sentence_break(remaining, MAX_WORDS_PER_CARD)
            if bp <= 0:
                bp = remaining.find(' ', len(' '.join(r_words[:MAX_WORDS_PER_CARD])))
                if bp <= 0:
                    cards.append(self._make_card(remaining.strip()))
                    break

            fragment = remaining[:bp].strip()
            remaining = remaining[bp:].lstrip()
            if fragment:
                cards.append(self._make_card(fragment))

        return cards

    def _find_sentence_break(self, text: str, max_words: int) -> int:
        """Find the best sentence-ending break point within max_words."""
        words = text.split()
        if len(words) <= max_words:
            return -1

        target_chars = len(' '.join(words[:max_words]))
        search_region = text[:target_chars + 50]

        best = -1
        for m in re.finditer(r'[.!?]\s', search_region):
            pos = m.end()
            preceding_words = len(text[:pos].split())
            if preceding_words >= MIN_WORDS_TO_FLUSH and preceding_words <= max_words + 5:
                best = pos

        return best

    def _make_card(self, text: str, is_whiteboard: bool = False) -> Card:
        idx = len(self.cards)
        card = Card(
            card_id=str(uuid.uuid4())[:8],
            text=text.strip(),
            index=idx,
            is_whiteboard=is_whiteboard,
            is_continuation=self._is_continuation and idx == 0,
        )
        self.cards.append(card)
        return card


def card_to_message(card: Card, msg_type: str = "card_push") -> dict:
    """Serialize a Card to a WebSocket JSON message."""
    return {
        "type": msg_type,
        "card_id": card.card_id,
        "text": card.text,
        "index": card.index,
        "total": card.total,
        "is_whiteboard": card.is_whiteboard,
        "is_continuation": card.is_continuation,
        "is_final": card.is_final,
    }
