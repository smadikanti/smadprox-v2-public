"""
Card Splitter — segments coaching answers into relay-ready cards.

Streaming text from the coaching engine is split on paragraph boundaries,
[WHITEBOARD]/[SAY] markers, and word-count thresholds. Cards are pushed
to the candidate overlay via /ws/overlay/{session_id}.

v2: First card appears IMMEDIATELY on first token, then grows via card_update.
    No waiting for a full paragraph before showing content.
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
    is_filler: bool = False


@dataclass
class CardBuffer:
    """
    Accumulates streaming tokens and emits cards.

    v2 behavior:
    - First card is created immediately on the first token (card_push)
    - As more tokens stream, the current card is updated (card_update)
    - When a paragraph break or word limit is hit, the current card finalizes
      and a new card is created
    - This means the candidate sees text appearing in real-time, not after
      a full paragraph is generated
    """

    cards: list[Card] = field(default_factory=list)
    _buffer: str = ""
    _is_continuation: bool = False
    _current_card_id: str = ""
    _current_card_text: str = ""

    def reset(self, is_continuation: bool = False):
        self.cards = []
        self._buffer = ""
        self._is_continuation = is_continuation
        self._current_card_id = ""
        self._current_card_text = ""

    def feed(self, chunk: str) -> list[dict]:
        """
        Feed a streaming chunk. Returns a list of actions:
        - {"action": "push", "card": Card}  — new card to display
        - {"action": "update", "card_id": str, "text": str}  — update existing card text
        - {"action": "finalize", "card_id": str, "text": str}  — card is complete
        """
        self._buffer += chunk
        actions = []

        # If no current card yet, create one immediately
        if not self._current_card_id and self._buffer.strip():
            card = self._make_card(self._buffer.strip())
            self._current_card_id = card.card_id
            self._current_card_text = self._buffer.strip()
            actions.append({"action": "push", "card": card})
            return actions

        # Check for paragraph breaks in the buffer
        while True:
            parts = re.split(r'\n\s*\n', self._buffer, maxsplit=1)
            if len(parts) < 2:
                break

            paragraph = parts[0].strip()
            self._buffer = parts[1]

            if not paragraph:
                continue

            # Finalize current card with this paragraph's text
            if self._current_card_id:
                actions.append({
                    "action": "finalize",
                    "card_id": self._current_card_id,
                    "text": paragraph,
                })
                # Update the card object too
                for c in self.cards:
                    if c.card_id == self._current_card_id:
                        c.text = paragraph
                        break
                self._current_card_id = ""
                self._current_card_text = ""

            # If there's remaining buffer, start a new card
            remaining = self._buffer.strip()
            if remaining:
                card = self._make_card(remaining)
                self._current_card_id = card.card_id
                self._current_card_text = remaining
                actions.append({"action": "push", "card": card})

        # Check word count — split if too long
        if self._current_card_id:
            current_words = len(self._buffer.strip().split())
            if current_words > MAX_WORDS_PER_CARD * 1.5:
                bp = self._find_sentence_break(self._buffer.strip(), MAX_WORDS_PER_CARD)
                if bp > 0:
                    fragment = self._buffer[:bp].strip()
                    self._buffer = self._buffer[bp:].lstrip()

                    # Finalize current card
                    actions.append({
                        "action": "finalize",
                        "card_id": self._current_card_id,
                        "text": fragment,
                    })
                    for c in self.cards:
                        if c.card_id == self._current_card_id:
                            c.text = fragment
                            break
                    self._current_card_id = ""
                    self._current_card_text = ""

                    # Start new card with remaining
                    remaining = self._buffer.strip()
                    if remaining:
                        card = self._make_card(remaining)
                        self._current_card_id = card.card_id
                        self._current_card_text = remaining
                        actions.append({"action": "push", "card": card})

            # Update current card with latest text (if text changed)
            elif self._buffer.strip() and self._buffer.strip() != self._current_card_text:
                self._current_card_text = self._buffer.strip()
                actions.append({
                    "action": "update",
                    "card_id": self._current_card_id,
                    "text": self._current_card_text,
                })

        return actions

    def finalize(self) -> list[dict]:
        """Flush remaining buffer as the final card."""
        actions = []
        text = self._buffer.strip()
        if text and self._current_card_id:
            actions.append({
                "action": "finalize",
                "card_id": self._current_card_id,
                "text": text,
            })
            for c in self.cards:
                if c.card_id == self._current_card_id:
                    c.text = text
                    c.is_final = True
                    break
        elif text:
            # Buffer has text but no current card — create final card
            card = self._make_card(text)
            card.is_final = True
            actions.append({"action": "push", "card": card})

        self._current_card_id = ""
        self._current_card_text = ""
        self._buffer = ""

        # Set totals on all cards
        total = len(self.cards)
        for c in self.cards:
            c.total = total

        return actions

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
