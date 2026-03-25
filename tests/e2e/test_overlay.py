"""
End-to-end overlay tests: card rendering in the Electron overlay window.

These tests verify that the Electron overlay correctly renders cards,
applies CSS classes (.current, .previous, .demoted), and handles the
card lifecycle — all via Chrome DevTools Protocol (CDP).

Requires:
    - Electron app running with --remote-debugging-port (electron_app fixture)
    - httpx and websockets installed
    - pytest-asyncio installed

Run:
    pytest tests/e2e/test_overlay.py -m e2e -v --timeout=60
"""

import asyncio
import json
import os
import sys

import httpx
import pytest
import websockets

# ---------------------------------------------------------------------------
# Ensure backend is importable if needed
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

# ---------------------------------------------------------------------------
# All tests in this module are async and e2e
# ---------------------------------------------------------------------------
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.asyncio,
]


# ---------------------------------------------------------------------------
# CDP helpers
# ---------------------------------------------------------------------------

async def _find_overlay_page(devtools_url: str) -> dict:
    """Find the overlay page from the Chrome DevTools page list."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{devtools_url}/json", timeout=5.0)
        resp.raise_for_status()
        pages = resp.json()

    # Look for the overlay page by URL
    for page in pages:
        url = page.get("url", "")
        title = page.get("title", "")
        if "overlay" in url.lower() or "overlay" in title.lower():
            return page

    # If no overlay-specific page found, list what we have for debugging
    available = [f"{p.get('title', '?')} ({p.get('url', '?')})" for p in pages]
    pytest.skip(
        f"No overlay page found in Electron DevTools. "
        f"Available pages: {available}"
    )


async def evaluate_in_overlay(devtools_url: str, expression: str):
    """Evaluate a JavaScript expression in the overlay page via CDP.

    Returns the evaluated value (Python-native via returnByValue).
    """
    page = await _find_overlay_page(devtools_url)
    ws_url = page["webSocketDebuggerUrl"]

    async with websockets.connect(ws_url, max_size=10 * 1024 * 1024) as ws:
        msg = {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": False,
            },
        }
        await ws.send(json.dumps(msg))
        resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))

        result = resp.get("result", {}).get("result", {})
        if result.get("type") == "undefined":
            return None
        return result.get("value")


async def inject_test_cards(devtools_url: str, cards: list[dict]):
    """Inject cards into the overlay by calling the renderer's addCard()
    function directly via CDP."""
    for card in cards:
        js = f"addCard({json.dumps(card)})"
        await evaluate_in_overlay(devtools_url, js)
    # Small delay for DOM to settle
    await asyncio.sleep(0.2)


async def clear_overlay_cards(devtools_url: str):
    """Clear all cards from the overlay feed."""
    await evaluate_in_overlay(devtools_url, "clearCards()")
    await asyncio.sleep(0.1)


# ---------------------------------------------------------------------------
# Test card data
# ---------------------------------------------------------------------------

SAMPLE_CARDS = [
    {
        "card_id": "test-card-1",
        "text": "Start by describing your most recent role and how it connects to this position.",
        "index": 1,
        "total": 3,
        "is_filler": False,
        "is_operator": False,
        "is_whiteboard": False,
        "is_continuation": False,
        "is_final": False,
    },
    {
        "card_id": "test-card-2",
        "text": "Mention the key project where you led the migration from monolith to microservices.",
        "index": 2,
        "total": 3,
        "is_filler": False,
        "is_operator": False,
        "is_whiteboard": False,
        "is_continuation": False,
        "is_final": False,
    },
    {
        "card_id": "test-card-3",
        "text": "Close with your excitement about the team's focus on real-time systems.",
        "index": 3,
        "total": 3,
        "is_filler": False,
        "is_operator": False,
        "is_whiteboard": False,
        "is_continuation": False,
        "is_final": True,
    },
]

FILLER_CARD = {
    "card_id": "test-filler-1",
    "text": "That's a great question. Let me think about the best way to frame this.",
    "index": 0,
    "total": 0,
    "is_filler": True,
    "is_operator": False,
    "is_whiteboard": False,
    "is_continuation": False,
    "is_final": False,
    "instruction": "You can say this while we prepare your answer",
    "estimated_seconds": 3,
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOverlayRendering:
    """Verify card rendering and CSS class management in the Electron overlay."""

    async def test_overlay_loads(self, electron_app):
        """The overlay page should be accessible via Chrome DevTools Protocol."""
        page = await _find_overlay_page(electron_app)
        assert page is not None
        assert "webSocketDebuggerUrl" in page

        # Verify we can evaluate JS in the page
        result = await evaluate_in_overlay(electron_app, "document.title")
        assert result is not None

    async def test_cards_render(self, electron_app):
        """Injecting cards via addCard() should create .feed-card DOM elements."""
        await clear_overlay_cards(electron_app)

        await inject_test_cards(electron_app, SAMPLE_CARDS)

        # Count .feed-card elements in the DOM
        count = await evaluate_in_overlay(
            electron_app,
            "document.querySelectorAll('.feed-card').length",
        )
        assert count == len(SAMPLE_CARDS), (
            f"Expected {len(SAMPLE_CARDS)} .feed-card elements, got {count}"
        )

        # Verify each card has text content
        texts = await evaluate_in_overlay(
            electron_app,
            """
            Array.from(document.querySelectorAll('.feed-card .card-body'))
                .map(el => el.textContent.trim())
            """,
        )
        assert len(texts) == len(SAMPLE_CARDS)
        for i, card in enumerate(SAMPLE_CARDS):
            assert card["text"] in texts[i], (
                f"Card {i} text mismatch: expected {card['text']!r}, got {texts[i]!r}"
            )

    async def test_current_card_highlighted(self, electron_app):
        """The last card added should have the .current CSS class."""
        await clear_overlay_cards(electron_app)

        await inject_test_cards(electron_app, SAMPLE_CARDS)

        # The last card should be .current
        last_has_current = await evaluate_in_overlay(
            electron_app,
            """
            (() => {
                const cards = document.querySelectorAll('.feed-card');
                if (cards.length === 0) return false;
                return cards[cards.length - 1].classList.contains('current');
            })()
            """,
        )
        assert last_has_current is True, "Last card should have .current class"

        # Only one card should be .current
        current_count = await evaluate_in_overlay(
            electron_app,
            "document.querySelectorAll('.feed-card.current').length",
        )
        assert current_count == 1, (
            f"Expected exactly 1 .current card, got {current_count}"
        )

    async def test_previous_cards_dimmed(self, electron_app):
        """Older cards (not the current one) should have the .previous CSS class."""
        await clear_overlay_cards(electron_app)

        await inject_test_cards(electron_app, SAMPLE_CARDS)

        # All non-current cards should have .previous
        previous_count = await evaluate_in_overlay(
            electron_app,
            "document.querySelectorAll('.feed-card.previous').length",
        )
        total_count = await evaluate_in_overlay(
            electron_app,
            "document.querySelectorAll('.feed-card').length",
        )
        # previous_count should be total - 1 (all except .current)
        assert previous_count == total_count - 1, (
            f"Expected {total_count - 1} .previous cards, got {previous_count}. "
            f"Total cards: {total_count}"
        )

        # Verify that .previous cards do NOT also have .current
        overlap = await evaluate_in_overlay(
            electron_app,
            "document.querySelectorAll('.feed-card.previous.current').length",
        )
        assert overlap == 0, "No card should be both .previous and .current"

    async def test_filler_card_demoted(self, electron_app):
        """Injecting a filler card and then calling demoteCard() should add
        the .demoted CSS class and remove .current/.previous."""
        await clear_overlay_cards(electron_app)

        # Add filler card first, then a regular card
        await inject_test_cards(electron_app, [FILLER_CARD, SAMPLE_CARDS[0]])

        # Filler should exist as a .filler card
        filler_exists = await evaluate_in_overlay(
            electron_app,
            "document.querySelectorAll('.feed-card.filler').length > 0",
        )
        assert filler_exists is True, "Filler card should be in the DOM"

        # Demote the filler
        await evaluate_in_overlay(
            electron_app,
            f"demoteCard({json.dumps(FILLER_CARD['card_id'])})",
        )
        await asyncio.sleep(0.2)

        # Verify .demoted class is applied
        has_demoted = await evaluate_in_overlay(
            electron_app,
            f"""
            (() => {{
                const card = document.querySelector('.feed-card[data-card-id="{FILLER_CARD["card_id"]}"]');
                return card ? card.classList.contains('demoted') : false;
            }})()
            """,
        )
        assert has_demoted is True, "Filler card should have .demoted class after demoteCard()"

        # Demoted card should NOT be .current
        is_current = await evaluate_in_overlay(
            electron_app,
            f"""
            (() => {{
                const card = document.querySelector('.feed-card[data-card-id="{FILLER_CARD["card_id"]}"]');
                return card ? card.classList.contains('current') : false;
            }})()
            """,
        )
        assert is_current is False, "Demoted card should not have .current class"

        # Demoted card should NOT be .previous (demoted takes priority)
        is_previous = await evaluate_in_overlay(
            electron_app,
            f"""
            (() => {{
                const card = document.querySelector('.feed-card[data-card-id="{FILLER_CARD["card_id"]}"]');
                return card ? card.classList.contains('previous') : false;
            }})()
            """,
        )
        assert is_previous is False, "Demoted card should not have .previous class"

        # The label should be updated to indicate demotion
        label_text = await evaluate_in_overlay(
            electron_app,
            f"""
            (() => {{
                const card = document.querySelector('.feed-card[data-card-id="{FILLER_CARD["card_id"]}"]');
                const label = card ? card.querySelector('.card-label') : null;
                return label ? label.textContent : '';
            }})()
            """,
        )
        assert "bridge" in label_text.lower(), (
            f"Demoted filler label should contain 'bridge', got {label_text!r}"
        )
