"""
Audio Pipeline for NoHuman.

Handles:
1. Twilio MediaStream → raw audio
2. Raw audio → Deepgram Flux (WebSocket)
3. Deepgram events → Three-Phase Filler Pipeline → Browser teleprompter

Uses direct WebSocket to Deepgram v2 API (not SDK) for full control.
"""

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Union
import websockets
from fastapi import WebSocket

from app.config import settings
from app.filler_engine import (
    FillerEngine,
    ImperfectionEngine,
    classify_segment,
    detect_conversation_type,
)
from app.coach import (
    generate_coaching,
    generate_suggestion,
    generate_dual_coaching,
    generate_groq_flash,
    generate_groq_quick_answer,
    detect_quick_answer,
    groq_available,
    build_strategy_context,
    classify_question,
    compress_conversation_state,
)
from app.highlight import SuggestionTracker
from app.tts import elevenlabs_available, tts_to_base64_chunks
from app.card_splitter import CardBuffer, card_to_message
from app import supabase_client as db

logger = logging.getLogger("nohuman.pipeline")


# ---------------------------------------------------------------------------
# Session State
# ---------------------------------------------------------------------------

@dataclass
class ActiveSession:
    """Holds the state for one active coaching session (NoHuman SaaS)."""

    session_id: str
    user_id: str
    context_id: int
    context_docs: list[dict] = field(default_factory=list)
    custom_prompt: str = ""
    conversation_type: str = "general"

    # Connections
    browser_ws: Optional[WebSocket] = None
    deepgram_ws: Optional[object] = None
    twilio_ws: Optional[WebSocket] = None

    # State
    conversation: list[dict] = field(default_factory=list)
    is_active: bool = True
    current_transcript: str = ""
    generating_suggestion: bool = False

    # Speaker tracking via turn_index alternation
    candidate_parity: Optional[int] = None  # 0 or 1
    current_turn_index: int = -1
    last_turn_index: int = -1

    # Engines
    filler_engine: Optional[FillerEngine] = None
    imperfection_engine: Optional[ImperfectionEngine] = None

    # Tasks
    _suggestion_task: Optional[asyncio.Task] = None
    _deepgram_recv_task: Optional[asyncio.Task] = None

    # ── Continuation awareness (shared with DualSession) ──
    last_suggestion: str = ""
    candidate_progress: str = ""
    _candidate_current_turn: str = ""
    last_interviewer_text: str = ""

    # ── Highlight tracker (NoHuman only) ──
    _highlight_tracker: Optional[SuggestionTracker] = None
    _last_highlight_pos: int = -1

    # ── Strategy Engine (shared with DualSession) ──
    strategy_brief: str = ""
    seniority_level: str = "mid"
    round_type: str = "general"
    spoken_rules: str = ""

    # System design evolving state
    design_state: dict = field(default_factory=lambda: {
        "phases_covered": [],
        "current_phase": "",
        "whiteboard_content": "",
        "interviewer_reactions": [],
    })

    # Behavioral state
    stories_told: list[str] = field(default_factory=list)

    # Coding state
    coding_state: dict = field(default_factory=lambda: {
        "problem_understood": False,
        "approach_discussed": False,
        "coding_started": False,
        "testing_done": False,
    })

    # Structured conversation state (compressed every ~5 turns)
    convo_state: dict = field(default_factory=dict)
    _convo_turn_count: int = 0
    _compress_task: Optional[asyncio.Task] = None

    def __post_init__(self):
        self.filler_engine = FillerEngine(self.conversation_type)
        self.imperfection_engine = ImperfectionEngine()

    def get_speaker_label(self, turn_index: int) -> str:
        """Get human-readable label for a turn based on alternation."""
        if self.candidate_parity is None:
            parity = turn_index % 2
            return "Speaker A" if parity == 0 else "Speaker B"
        parity = turn_index % 2
        if parity == self.candidate_parity:
            return "You"
        return "Them"

    def is_other_person_turn(self, turn_index: int) -> bool:
        """Check if this turn belongs to the other person (not candidate)."""
        if self.candidate_parity is None:
            return True  # Before assignment, coach on all turns
        return (turn_index % 2) != self.candidate_parity

    def swap_speakers(self) -> None:
        """Swap who is candidate vs interviewer."""
        if self.candidate_parity is None:
            # Default: assume current turn parity is candidate, swap to other
            self.candidate_parity = 1 - (self.current_turn_index % 2)
        else:
            self.candidate_parity = 1 - self.candidate_parity
        logger.info(f"Swapped speakers: candidate_parity={self.candidate_parity}")

    def set_i_am_speaking(self) -> None:
        """User indicates they are currently speaking (marks current turn as candidate)."""
        self.candidate_parity = self.current_turn_index % 2
        logger.info(f"User marked as speaking: candidate_parity={self.candidate_parity}")


# Global session registry
active_sessions: dict[str, ActiveSession] = {}


# ---------------------------------------------------------------------------
# Browser WebSocket Communication
# ---------------------------------------------------------------------------

async def send_to_browser(session: ActiveSession, message: dict) -> None:
    """Send a JSON message to the browser teleprompter."""
    if session.browser_ws:
        try:
            await session.browser_ws.send_json(message)
        except Exception as e:
            logger.warning(f"Failed to send to browser: {e}")


# ---------------------------------------------------------------------------
# Deepgram Flux Connection
# ---------------------------------------------------------------------------

async def connect_deepgram(
    encoding: str = "mulaw",
    sample_rate: int = 8000,
    containerized: bool = False,
) -> websockets.WebSocketClientProtocol:
    """
    Connect to Deepgram Flux v2 API via WebSocket.
    Returns the WebSocket connection.
    """
    params = "model=flux-general-en"
    if not containerized:
        params += f"&encoding={encoding}&sample_rate={sample_rate}"

    url = f"wss://api.deepgram.com/v2/listen?{params}"
    headers = {"Authorization": f"Token {settings.DEEPGRAM_API_KEY}"}

    ws = await websockets.connect(url, additional_headers=headers)
    logger.info(f"Connected to Deepgram Flux: {url}")
    return ws


async def deepgram_receiver(session: ActiveSession) -> None:
    """
    Receive and process events from Deepgram Flux v2 API.

    Flux v2 wraps events in type="TurnInfo" with an "event" field:
      - event: "Update"         → streaming transcript update
      - event: "StartOfTurn"    → speech started
      - event: "EagerEndOfTurn" → early end-of-turn signal
      - event: "TurnResumed"    → user kept speaking after EagerEOT
      - event: "EndOfTurn"      → confirmed end of turn
    Also receives type="Connected" on initial connection.
    """
    if not session.deepgram_ws:
        return

    try:
        async for raw_message in session.deepgram_ws:
            if not session.is_active:
                break

            event = json.loads(raw_message)
            msg_type = event.get("type", "")

            # Connection confirmation
            if msg_type == "Connected":
                logger.info(f"Deepgram connected for session {session.session_id}")
                await send_to_browser(session, {
                    "type": "status",
                    "status": "connected",
                    "message": "Listening...",
                })
                continue

            # All Flux v2 speech events come as type="TurnInfo"
            if msg_type != "TurnInfo":
                logger.debug(f"Deepgram unknown message type: {msg_type}")
                continue

            flux_event = event.get("event", "")
            transcript = event.get("transcript", "")
            turn_index = event.get("turn_index", 0)

            # Track current turn
            session.current_turn_index = turn_index
            speaker_label = session.get_speaker_label(turn_index)

            if flux_event in ("Update", "StartOfTurn"):
                # Streaming transcript — forward to browser for live display
                if transcript:
                    session.current_transcript = transcript
                    # Track streaming candidate text for continuation awareness
                    if speaker_label == "You":
                        session._candidate_current_turn = transcript

                        # ── Highlight tracking: match spoken words against suggestion ──
                        if session._highlight_tracker:
                            pos = session._highlight_tracker.update(transcript)
                            if pos != session._last_highlight_pos:
                                session._last_highlight_pos = pos
                                await send_to_browser(session, {
                                    "type": "highlight",
                                    "position": pos,
                                    "total": session._highlight_tracker.word_count,
                                })

                    await send_to_browser(session, {
                        "type": "transcript",
                        "text": transcript,
                        "is_final": False,
                        "speaker": speaker_label,
                        "turn_index": turn_index,
                    })

            elif flux_event == "EagerEndOfTurn":
                # Early signal — we can start preparing
                logger.info(
                    f"EagerEndOfTurn [{speaker_label}] turn={turn_index} "
                    f"for session {session.session_id}: {transcript[:60]}..."
                )
                if transcript:
                    session.current_transcript = transcript
                    await send_to_browser(session, {
                        "type": "transcript",
                        "text": transcript,
                        "is_final": False,
                        "speaker": speaker_label,
                        "turn_index": turn_index,
                    })

            elif flux_event == "EndOfTurn":
                # Confirmed end of turn
                logger.info(
                    f"EndOfTurn [{speaker_label}] turn={turn_index} "
                    f"for session {session.session_id}: {transcript[:80]}..."
                )
                if transcript:
                    session.current_transcript = transcript
                    # Save final transcript with speaker info
                    role = "transcript"
                    if speaker_label == "You":
                        role = "candidate"
                    elif speaker_label == "Them":
                        role = "interviewer"
                    session.conversation.append({
                        "role": role,
                        "content": transcript,
                    })
                    await send_to_browser(session, {
                        "type": "transcript",
                        "text": transcript,
                        "is_final": True,
                        "speaker": speaker_label,
                        "turn_index": turn_index,
                    })

                    # ── Continuation awareness tracking ──
                    if role == "candidate":
                        if session.candidate_progress:
                            session.candidate_progress += " " + transcript
                        else:
                            session.candidate_progress = transcript
                        session._candidate_current_turn = ""
                    elif role == "interviewer":
                        session.last_interviewer_text = transcript

                # Trigger periodic conversation state compression
                asyncio.create_task(_maybe_compress_convo(session))

                # Only generate suggestions when the OTHER person finishes
                if session.is_other_person_turn(turn_index):
                    await handle_end_of_turn(session)
                else:
                    logger.info(
                        f"Skipping suggestion — candidate was speaking "
                        f"(turn={turn_index}, parity={session.candidate_parity})"
                    )

                session.last_turn_index = turn_index

            elif flux_event == "TurnResumed":
                # User kept speaking after EagerEndOfTurn — cancel suggestion
                logger.info(f"TurnResumed for session {session.session_id}")
                if session._suggestion_task and not session._suggestion_task.done():
                    session._suggestion_task.cancel()
                    session.generating_suggestion = False
                    await send_to_browser(session, {"type": "suggestion_cancelled"})
                if transcript:
                    session.current_transcript = transcript
                    await send_to_browser(session, {
                        "type": "transcript",
                        "text": transcript,
                        "is_final": False,
                        "speaker": speaker_label,
                        "turn_index": turn_index,
                    })

    except websockets.exceptions.ConnectionClosed:
        logger.info(f"Deepgram connection closed for session {session.session_id}")
    except Exception as e:
        logger.error(f"Deepgram receiver error: {e}")


# ---------------------------------------------------------------------------
# Three-Phase Suggestion Pipeline
# ---------------------------------------------------------------------------

async def handle_end_of_turn(session: ActiveSession) -> None:
    """
    Fires the three-phase filler pipeline on EndOfTurn.

    Phase 1: Instant filler (<1ms)
    Phase 2: Bridge sentence (~200ms)
    Phase 3: Claude streaming suggestion (async task)
    """
    if not session.current_transcript or not session.filler_engine:
        return

    last_transcript = session.current_transcript

    # Skip very short segments (rapid back-and-forth)
    if len(last_transcript.split()) < 3:
        return

    # Cancel previous suggestion if still generating
    if session._suggestion_task and not session._suggestion_task.done():
        session._suggestion_task.cancel()
        await send_to_browser(session, {"type": "suggestion_cancelled"})

    # Classify the segment
    segment_type = classify_segment(last_transcript)

    # --- PHASE 1: Instant Filler (<1ms) ---
    phase1 = session.filler_engine.generate_phase1(segment_type)
    await send_to_browser(session, {
        "type": "filler",
        "phase": 1,
        "text": phase1,
    })

    # --- PHASE 2: Bridge Sentence (~200ms) ---
    phase2 = session.filler_engine.generate_phase2(last_transcript, segment_type)
    if phase2:
        await send_to_browser(session, {
            "type": "filler",
            "phase": 2,
            "text": phase2,
        })
    else:
        phase2 = ""

    # --- PHASE 3: Claude Streaming (async task) ---
    session._suggestion_task = asyncio.create_task(
        _generate_and_stream_suggestion(session, phase1, phase2 or "")
    )


async def _generate_and_stream_suggestion(
    session: ActiveSession,
    phase1_filler: str,
    phase2_bridge: str,
) -> None:
    """Generate Claude suggestion and stream to teleprompter.

    Uses the unified coaching pipeline with strategy engine, continuation
    awareness, round-type routing, and question-type detection — same quality
    as the HumanProx path.
    """
    # ── Generation gate: check free trial / credits ──
    if session.user_id:
        check = await db.check_can_generate(session.user_id)
        if not check["allowed"]:
            logger.warning(
                f"[Pipeline] Generation blocked for session {session.session_id}: "
                f"{check['reason']}"
            )
            await send_to_browser(session, {
                "type": "limit_reached",
                "message": check["reason"],
            })
            return

    session.generating_suggestion = True

    # ── Latency tracking ──
    t_start = time.monotonic()
    t_classify_done = 0.0
    t_first_token = 0.0
    provider_used = "claude"

    # Snapshot continuation state before generation
    prev_suggestion = session.last_suggestion
    candidate_said = session.candidate_progress
    interviewer_text = session.last_interviewer_text

    # Classify question type via Groq (~50ms) for progressive disclosure
    q_type = await classify_question(interviewer_text)
    t_classify_done = time.monotonic()

    # Build strategy context if strategy has been compiled
    strategy_ctx = None
    if session.strategy_brief:
        strategy_ctx = build_strategy_context(
            strategy_brief=session.strategy_brief,
            round_type=session.round_type,
            spoken_rules=session.spoken_rules,
            design_state=session.design_state,
            stories_told=session.stories_told,
            coding_state=session.coding_state,
        )

    try:
        # ── Quick-answer fast-path: Groq as PRIMARY responder ──
        if q_type == "quick_answer" and groq_available():
            logger.info(f"[Pipeline] Quick-answer detected, routing to Groq: {interviewer_text[:60]}")
            provider_used = "groq"

            culture_values = ""
            for doc in session.context_docs:
                if doc.get("doc_type") in ("culture_values", "culture"):
                    culture_values = doc.get("content", "")[:200]
                    break

            quick_answer = await generate_groq_quick_answer(
                conversation=session.conversation,
                last_interviewer_text=interviewer_text,
                context_docs=session.context_docs,
                strategy_brief=session.strategy_brief,
                seniority_level=session.seniority_level,
                spoken_rules=session.spoken_rules,
                culture_values=culture_values,
            )
            t_first_token = time.monotonic()

            if quick_answer:
                display_text = f"{phase1_filler} {quick_answer}" if phase1_filler else quick_answer

                await send_to_browser(session, {"type": "suggestion_start"})
                await send_to_browser(session, {
                    "type": "suggestion_chunk",
                    "text": display_text,
                })

                session.conversation.append({
                    "role": "suggestion",
                    "content": display_text,
                })
                session.last_suggestion = quick_answer
                session.candidate_progress = ""
                session._candidate_current_turn = ""

                session._highlight_tracker = SuggestionTracker(display_text)
                session._last_highlight_pos = -1

                t_end = time.monotonic()
                await send_to_browser(session, {
                    "type": "suggestion_end",
                    "full_text": display_text,
                })
                _report = {
                    "type": "latency_report",
                    "provider": "groq",
                    "question_type": q_type,
                    "classify_ms": round((t_classify_done - t_start) * 1000),
                    "ttft_ms": round((t_first_token - t_start) * 1000),
                    "total_ms": round((t_end - t_start) * 1000),
                    "word_count": len(quick_answer.split()),
                }
                logger.info(f"[Pipeline] Latency: {_report}")
                await send_to_browser(session, _report)

                if session.user_id:
                    check = await db.check_can_generate(session.user_id)
                    if check["source"] == "free_trial":
                        remaining = await db.use_free_generation(session.user_id)
                        await send_to_browser(session, {
                            "type": "free_trial_update",
                            "remaining": remaining,
                        })

                session.generating_suggestion = False
                return
            logger.info("[Pipeline] Groq quick-answer failed, falling back to Claude")
            provider_used = "claude"

        await send_to_browser(session, {"type": "suggestion_start"})

        full_text = ""
        async for chunk in generate_coaching(
            context_docs=session.context_docs,
            conversation=session.conversation,
            custom_prompt=session.custom_prompt,
            last_suggestion=prev_suggestion,
            candidate_progress=candidate_said,
            last_interviewer_text=interviewer_text,
            strategy_ctx=strategy_ctx,
            filler_bridge=(phase1_filler, phase2_bridge),
            question_type=q_type,
            convo_state=session.convo_state,
        ):
            if not session.is_active:
                break

            if not full_text:
                t_first_token = time.monotonic()

            full_text += chunk
            await send_to_browser(session, {
                "type": "suggestion_chunk",
                "text": chunk,
            })

        # Apply controlled imperfection
        if session.imperfection_engine and session.filler_engine:
            full_text = session.imperfection_engine.calibrate(
                full_text, session.filler_engine.turn_count
            )

        # Save suggestion to conversation history
        session.conversation.append({
            "role": "suggestion",
            "content": f"{phase1_filler} {phase2_bridge} {full_text}",
        })

        # Update continuation state for next cycle
        session.last_suggestion = full_text
        session.candidate_progress = ""
        session._candidate_current_turn = ""

        session._highlight_tracker = SuggestionTracker(full_text)
        session._last_highlight_pos = -1

        # Update round-specific evolving state
        if session.round_type == "system_design":
            _update_design_state(session, full_text, interviewer_text)
        elif session.round_type == "behavioral":
            _update_behavioral_state(session, full_text)
        elif session.round_type == "technical_coding":
            _update_coding_state(session, full_text, interviewer_text)

        t_end = time.monotonic()
        await send_to_browser(session, {
            "type": "suggestion_end",
            "full_text": full_text,
        })
        _report = {
            "type": "latency_report",
            "provider": provider_used,
            "question_type": q_type,
            "classify_ms": round((t_classify_done - t_start) * 1000),
            "ttft_ms": round((t_first_token - t_start) * 1000) if t_first_token else 0,
            "total_ms": round((t_end - t_start) * 1000),
            "word_count": len(full_text.split()),
        }
        logger.info(f"[Pipeline] Latency: {_report}")
        await send_to_browser(session, _report)

        # ── Deduct free generation if using free trial ──
        if session.user_id:
            check = await db.check_can_generate(session.user_id)
            if check["source"] == "free_trial":
                remaining = await db.use_free_generation(session.user_id)
                logger.info(
                    f"[Pipeline] Free generation used for {session.session_id}, "
                    f"{remaining} remaining"
                )
                await send_to_browser(session, {
                    "type": "free_trial_update",
                    "remaining": remaining,
                })

    except asyncio.CancelledError:
        logger.info("Suggestion generation cancelled (new speech detected)")
    except Exception as e:
        logger.error(f"Suggestion generation error: {e}")
        await send_to_browser(session, {
            "type": "error",
            "message": f"Failed to generate suggestion: {str(e)}",
        })
    finally:
        session.generating_suggestion = False


# ---------------------------------------------------------------------------
# Audio Forwarding
# ---------------------------------------------------------------------------

async def forward_twilio_audio(session: ActiveSession, payload: str) -> None:
    """
    Forward Twilio MediaStream audio to Deepgram.
    Twilio sends base64-encoded mulaw at 8kHz.
    """
    if session.deepgram_ws:
        try:
            audio_bytes = base64.b64decode(payload)
            await session.deepgram_ws.send(audio_bytes)
        except Exception as e:
            logger.error(f"Failed to forward audio: {e}")


async def forward_browser_audio(session: ActiveSession, audio_data: bytes) -> None:
    """
    Forward browser microphone audio to Deepgram.
    Browser sends WebM Opus chunks as binary.
    """
    if session.deepgram_ws:
        try:
            await session.deepgram_ws.send(audio_data)
        except Exception as e:
            logger.error(f"Failed to forward browser audio: {e}")


# ---------------------------------------------------------------------------
# Session Lifecycle
# ---------------------------------------------------------------------------

async def start_session_pipeline(
    session: ActiveSession,
    audio_source: str = "browser",
) -> None:
    """
    Start the Deepgram connection and receiver for a session.
    audio_source: 'browser' (WebM Opus) or 'twilio' (mulaw 8kHz)
    """
    try:
        if audio_source == "twilio":
            session.deepgram_ws = await connect_deepgram(
                encoding="mulaw", sample_rate=8000
            )
        else:
            # Browser sends WebM Opus (containerized)
            session.deepgram_ws = await connect_deepgram(containerized=True)

        # Detect conversation type from context docs
        conv_type = detect_conversation_type(session.context_docs)
        session.conversation_type = conv_type
        if session.filler_engine:
            session.filler_engine.set_conversation_type(conv_type)

        # Start the receiver task
        session._deepgram_recv_task = asyncio.create_task(
            deepgram_receiver(session)
        )

        logger.info(
            f"Pipeline started for session {session.session_id} "
            f"(type={conv_type}, source={audio_source})"
        )

    except Exception as e:
        logger.error(f"Failed to start pipeline: {e}")
        await send_to_browser(session, {
            "type": "error",
            "message": f"Failed to connect to speech recognition: {str(e)}",
        })


async def stop_session_pipeline(session: ActiveSession) -> None:
    """Clean up all connections for a session."""
    session.is_active = False

    # Cancel suggestion task
    if session._suggestion_task and not session._suggestion_task.done():
        session._suggestion_task.cancel()

    # Cancel receiver task
    if session._deepgram_recv_task and not session._deepgram_recv_task.done():
        session._deepgram_recv_task.cancel()

    # Close Deepgram connection
    if session.deepgram_ws:
        try:
            await session.deepgram_ws.send(json.dumps({"type": "CloseStream"}))
            await session.deepgram_ws.close()
        except Exception:
            pass

    # Remove from registry
    if session.session_id in active_sessions:
        del active_sessions[session.session_id]

    logger.info(f"Pipeline stopped for session {session.session_id}")


# ===========================================================================
# DUAL-SOURCE SESSION (Mac system audio + Twilio phone)
# ===========================================================================

@dataclass
class DualSession:
    """Holds state for a dual-source session: Mac (interviewer) + Twilio (candidate)."""

    session_id: str
    user_id: str = ""

    # Interviewer stream (system audio from Mac Electron)
    interviewer_dg_ws: Optional[object] = None
    interviewer_recv_task: Optional[asyncio.Task] = None

    # Candidate stream (Twilio phone call)
    candidate_dg_ws: Optional[object] = None
    candidate_recv_task: Optional[asyncio.Task] = None

    # Dashboard viewers (WebSocket connections)
    dashboard_viewers: list = field(default_factory=list)

    # Overlay viewers — candidate NoScreen apps receiving cards
    overlay_viewers: list = field(default_factory=list)
    overlay_auto_relay: bool = False

    # Conversation history
    conversation: list[dict] = field(default_factory=list)
    is_active: bool = True

    # Context for suggestion generation
    context_docs: list[dict] = field(default_factory=list)
    custom_prompt: str = ""

    # Suggestion state
    _suggestion_task: Optional[asyncio.Task] = None
    generating_suggestion: bool = False

    # Continuation-awareness state
    last_suggestion: str = ""              # Full text of last Claude suggestion
    candidate_progress: str = ""           # What candidate said since last suggestion
    _candidate_current_turn: str = ""      # Current candidate turn (streaming, not yet finalized)
    last_interviewer_text: str = ""        # Last interviewer utterance

    # Candidate metadata (set by candidate-lite app via register message)
    candidate_name: str = ""
    candidate_context: dict = field(default_factory=dict)
    connected_at: float = 0.0              # time.time() when session was created/connected

    # HumanProx mode: "nohuman" (default) or "humanprox"
    mode: str = "nohuman"
    tts_enabled: bool = False
    tts_voice_id: str = ""                 # Override voice; empty = use settings default
    tts_auto: bool = True                  # True = auto-play TTS; False = supervision (manual play)
    gen_auto: bool = True                  # True = auto-generate on EndOfTurn; False = manual trigger
    _pending_interviewer_text: str = ""    # Buffered text for manual-gen mode

    # ── Strategy Engine (set after strategy compilation) ──
    strategy_brief: str = ""
    seniority_level: str = "mid"
    round_type: str = "general"
    spoken_rules: str = ""

    # System design evolving state
    design_state: dict = field(default_factory=lambda: {
        "phases_covered": [],
        "current_phase": "",
        "whiteboard_content": "",
        "interviewer_reactions": [],
    })

    # Behavioral state
    stories_told: list[str] = field(default_factory=list)

    # Coding state
    coding_state: dict = field(default_factory=lambda: {
        "problem_understood": False,
        "approach_discussed": False,
        "coding_started": False,
        "testing_done": False,
    })

    # Structured conversation state (compressed every ~5 turns)
    convo_state: dict = field(default_factory=dict)
    _convo_turn_count: int = 0
    _compress_task: Optional[asyncio.Task] = None


# Global registry for dual sessions
dual_sessions: dict[str, DualSession] = {}


async def send_to_dashboard(session: DualSession, message: dict) -> None:
    """Send a JSON message to all dashboard viewers."""
    dead = []
    for ws in session.dashboard_viewers:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        session.dashboard_viewers.remove(ws)


async def send_to_overlay(session: DualSession, message: dict) -> None:
    """Send a JSON message to all candidate overlay (NoScreen) viewers."""
    dead = []
    for ws in session.overlay_viewers:
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        session.overlay_viewers.remove(ws)


COMPRESS_EVERY_N_TURNS = 5


async def _maybe_compress_convo(session) -> None:
    """Trigger Groq conversation compression every N turns (non-blocking)."""
    session._convo_turn_count += 1
    if session._convo_turn_count % COMPRESS_EVERY_N_TURNS != 0:
        return
    if session._compress_task and not session._compress_task.done():
        return

    async def _do_compress():
        try:
            session.convo_state = await compress_conversation_state(
                session.conversation, session.convo_state
            )
        except Exception as e:
            logger.warning(f"Conversation compression error: {e}")

    session._compress_task = asyncio.create_task(_do_compress())


async def _save_event(session_id: str, role: str, content: str) -> None:
    """Persist an interview event (transcript/suggestion) to the messages table."""
    try:
        await db.save_message(session_id, role, content)
    except Exception as e:
        logger.warning(f"[Dual] Failed to save {role} event for {session_id}: {e}")


async def _stream_tts_to_dashboard(session: DualSession, text: str) -> None:
    """Stream ElevenLabs TTS audio chunks to all dashboard viewers as base64 PCM."""
    from app.tts import TTS_SAMPLE_RATE
    voice = session.tts_voice_id or None
    try:
        await send_to_dashboard(session, {
            "type": "tts_start",
            "format": "pcm",
            "sample_rate": TTS_SAMPLE_RATE,
        })
        chunk_count = 0
        async for b64_chunk in tts_to_base64_chunks(text, voice_id=voice):
            if not session.is_active:
                break
            await send_to_dashboard(session, {
                "type": "tts_chunk",
                "audio": b64_chunk,
            })
            chunk_count += 1
        await send_to_dashboard(session, {"type": "tts_end"})
        logger.info(
            f"[TTS] Streamed {chunk_count} PCM chunks for {session.session_id}"
        )
    except Exception as e:
        logger.error(f"[TTS] Error streaming for {session.session_id}: {e}")
        await send_to_dashboard(session, {"type": "tts_end"})


async def dual_deepgram_receiver(
    session: DualSession,
    dg_ws,
    speaker: str,
) -> None:
    """
    Receive Deepgram Flux v2 events and forward to dashboard.
    speaker: 'interviewer' or 'candidate'
    """
    try:
        async for raw_message in dg_ws:
            if not session.is_active:
                break

            event = json.loads(raw_message)
            msg_type = event.get("type", "")

            if msg_type == "Connected":
                logger.info(f"[Dual:{speaker}] Deepgram connected for {session.session_id}")
                await send_to_dashboard(session, {
                    "type": "status",
                    "speaker": speaker,
                    "status": "connected",
                })
                continue

            if msg_type != "TurnInfo":
                continue

            flux_event = event.get("event", "")
            transcript = event.get("transcript", "")
            turn_index = event.get("turn_index", 0)

            if not transcript:
                continue

            is_final = flux_event == "EndOfTurn"

            if is_final:
                session.conversation.append({
                    "role": speaker,
                    "content": transcript,
                })
                logger.info(
                    f"[Dual:{speaker}] EndOfTurn t={turn_index}: "
                    f"{transcript[:80]}..."
                )
                # Persist to DB for iterative improvement
                asyncio.create_task(_save_event(session.session_id, speaker, transcript))
                # Trigger periodic conversation state compression
                asyncio.create_task(_maybe_compress_convo(session))

            # ── Track candidate progress for continuation awareness ──
            if speaker == "candidate":
                if is_final:
                    # Append finalized turn to candidate_progress
                    if session.candidate_progress:
                        session.candidate_progress += " " + transcript
                    else:
                        session.candidate_progress = transcript
                    session._candidate_current_turn = ""
                else:
                    # Track streaming text for the current turn
                    session._candidate_current_turn = transcript

            # Send to dashboard
            await send_to_dashboard(session, {
                "type": "transcript",
                "speaker": speaker,
                "text": transcript,
                "is_final": is_final,
                "event": flux_event,
                "turn_index": turn_index,
            })

            # Trigger suggestion when INTERVIEWER finishes speaking
            if is_final and speaker == "interviewer":
                # Cancel any in-progress suggestion
                if session._suggestion_task and not session._suggestion_task.done():
                    session._suggestion_task.cancel()
                    await send_to_dashboard(session, {"type": "suggestion_cancelled"})

                # Store last interviewer text
                session.last_interviewer_text = transcript

                # Build candidate progress (include any in-progress turn)
                candidate_said = session.candidate_progress
                if session._candidate_current_turn:
                    if candidate_said:
                        candidate_said += " " + session._candidate_current_turn
                    else:
                        candidate_said = session._candidate_current_turn

                # Only generate if transcript is meaningful (>3 words)
                if len(transcript.split()) >= 3:
                    if session.gen_auto:
                        session._suggestion_task = asyncio.create_task(
                            generate_dual_suggestion(session, transcript)
                        )
                    else:
                        # Manual mode: buffer the question, notify dashboard
                        session._pending_interviewer_text = transcript
                        await send_to_dashboard(session, {
                            "type": "generation_ready",
                            "question": transcript,
                        })

            # If candidate starts speaking while suggestion is generating, cancel it
            if speaker == "candidate" and flux_event == "StartOfTurn":
                if session._suggestion_task and not session._suggestion_task.done():
                    session._suggestion_task.cancel()
                    session.generating_suggestion = False
                    await send_to_dashboard(session, {"type": "suggestion_cancelled"})

    except websockets.exceptions.ConnectionClosed:
        logger.info(f"[Dual:{speaker}] Deepgram closed for {session.session_id}")
    except Exception as e:
        logger.error(f"[Dual:{speaker}] Deepgram receiver error: {e}")


def _generate_instant_filler(q_type: str, question: str, session) -> str:
    """
    Generate an instant filler line based on question type.
    This runs synchronously (no API call) for <10ms latency.
    The filler gives the candidate something to say while Claude generates.
    """
    # Extract key terms from the question for specificity
    q_lower = question.lower()

    if q_type == "quick_answer":
        return ""  # Quick answers are fast enough, no filler needed

    if "tell me about yourself" in q_lower or "walk me through" in q_lower:
        return "Sure, so a bit about my background..."

    if "tell me about a time" in q_lower or "give me an example" in q_lower:
        if "conflict" in q_lower or "disagree" in q_lower:
            return "Yeah, so there was a situation where I had to navigate a disagreement on the team..."
        if "challenge" in q_lower or "difficult" in q_lower:
            return "So one situation that comes to mind is a pretty challenging technical problem we faced..."
        if "leader" in q_lower or "led" in q_lower:
            return "Yeah, so there was a project where I took the lead on the technical direction..."
        return "Yeah, so one situation that comes to mind..."

    if "design" in q_lower or "architect" in q_lower or "system" in q_lower:
        return "So let me think through the architecture here..."

    if "why" in q_lower and ("company" in q_lower or "role" in q_lower or "interested" in q_lower):
        return "Yeah, so what really drew me to this role..."

    if "code" in q_lower or "implement" in q_lower or "function" in q_lower:
        return "Alright, let me think through the approach here..."

    # Check if we have prior context to generate a more specific filler
    if session.last_suggestion:
        return "Right, so building on that..."

    return "Yeah, so thinking about this..."


async def generate_dual_suggestion(session: DualSession, last_interviewer_text: str) -> None:
    """
    Generate a continuation-aware Claude suggestion when the interviewer finishes speaking.
    Streams chunks to all dashboard viewers.

    Tracks what was previously suggested vs what the candidate actually said,
    so Claude can generate smart continuations instead of starting from scratch.
    """
    # ── Generation gate: check free trial / credits ──
    if session.user_id:
        check = await db.check_can_generate(session.user_id)
        if not check["allowed"]:
            logger.warning(
                f"[Dual] Generation blocked for session {session.session_id}: "
                f"{check['reason']}"
            )
            await send_to_dashboard(session, {
                "type": "limit_reached",
                "message": check["reason"],
            })
            return

    session.generating_suggestion = True

    # ── Metrics tracking ──
    from app.metrics import SessionMetricsTracker
    if not hasattr(session, '_metrics'):
        session._metrics = SessionMetricsTracker(session.session_id)
    metrics = session._metrics.start_question(last_interviewer_text)

    t_start = time.monotonic()
    t_classify_done = 0.0
    t_first_token = 0.0
    t_groq_flash_done = 0.0
    provider_used = "claude"

    # Determine if this is a continuation of a previous suggestion
    is_continuation = bool(session.last_suggestion)

    # Snapshot state before generation (may be reset during async work)
    prev_suggestion = session.last_suggestion
    candidate_said = session.candidate_progress
    interviewer_text = last_interviewer_text

    # Classify question type via Groq (~50ms) for progressive disclosure
    q_type = await classify_question(interviewer_text)
    t_classify_done = time.monotonic()
    metrics._t_classify_done = t_classify_done
    metrics.question_type = q_type
    metrics.classify_ms = round((t_classify_done - t_start) * 1000, 1)

    # ── INSTANT FILLER: push a bridge card within ~50ms of EndOfTurn ──
    # This is the key to <200ms TTFC — show SOMETHING immediately
    if session.overlay_viewers or session.dashboard_viewers:
        filler_text = _generate_instant_filler(q_type, interviewer_text, session)
        if filler_text:
            import uuid
            filler_id = "filler-" + str(uuid.uuid4())[:6]
            filler_msg = {
                "type": "card_push",
                "card_id": filler_id,
                "text": filler_text,
                "index": 0,
                "total": 0,
                "is_filler": True,
                "is_whiteboard": False,
                "is_continuation": False,
                "is_final": False,
                "instruction": "You can say this while we prepare your answer",
                "estimated_seconds": 3,
            }
            await send_to_dashboard(session, filler_msg)
            if session.overlay_viewers:
                await send_to_overlay(session, filler_msg)
            metrics.filler_generated = True
            metrics.filler_text = filler_text
            metrics.filler_source = "instant"
            metrics._t_filler_sent = time.monotonic()
            logger.info(
                f"[Dual] Filler sent in {round((metrics._t_filler_sent - t_start)*1000)}ms: "
                f"{filler_text[:60]}"
            )

    try:
        # ── Quick-answer fast-path: Groq as PRIMARY responder ──
        if q_type == "quick_answer" and groq_available():
            logger.info(f"[Dual] Quick-answer detected, routing to Groq: {interviewer_text[:60]}")
            provider_used = "groq"

            culture_values = ""
            for doc in session.context_docs:
                if doc.get("doc_type") in ("culture_values", "culture"):
                    culture_values = doc.get("content", "")[:200]
                    break

            quick_answer = await generate_groq_quick_answer(
                conversation=session.conversation,
                last_interviewer_text=interviewer_text,
                context_docs=session.context_docs,
                strategy_brief=session.strategy_brief,
                seniority_level=session.seniority_level,
                spoken_rules=session.spoken_rules,
                culture_values=culture_values,
            )
            t_first_token = time.monotonic()

            if quick_answer:
                await send_to_dashboard(session, {
                    "type": "suggestion_start",
                    "is_continuation": is_continuation,
                    "question": interviewer_text,
                    "quick_answer": True,
                })
                await send_to_dashboard(session, {
                    "type": "suggestion_chunk",
                    "text": quick_answer,
                })

                session.conversation.append({
                    "role": "suggestion",
                    "content": quick_answer,
                })
                session.last_suggestion = quick_answer
                session.candidate_progress = ""
                session._candidate_current_turn = ""

                t_end = time.monotonic()
                await send_to_dashboard(session, {
                    "type": "suggestion_end",
                    "full_text": quick_answer,
                    "is_continuation": is_continuation,
                    "quick_answer": True,
                })

                _report = {
                    "type": "latency_report",
                    "provider": "groq",
                    "question_type": q_type,
                    "classify_ms": round((t_classify_done - t_start) * 1000),
                    "ttft_ms": round((t_first_token - t_start) * 1000),
                    "total_ms": round((t_end - t_start) * 1000),
                    "word_count": len(quick_answer.split()),
                }
                logger.info(f"[Dual] Latency: {_report}")
                await send_to_dashboard(session, _report)

                asyncio.create_task(_save_event(session.session_id, "ai_suggestion", quick_answer))

                if session.user_id:
                    check = await db.check_can_generate(session.user_id)
                    if check["source"] == "free_trial":
                        remaining = await db.use_free_generation(session.user_id)
                        await send_to_dashboard(session, {
                            "type": "free_trial_update",
                            "remaining": remaining,
                        })

                session.generating_suggestion = False
                return
            # If Groq failed, fall through to Claude path below
            logger.info("[Dual] Groq quick-answer failed, falling back to Claude")
            provider_used = "claude"

        await send_to_dashboard(session, {
            "type": "suggestion_start",
            "is_continuation": is_continuation,
            "question": interviewer_text,
        })

        # Prepare card buffer for overlay viewers
        card_buf = CardBuffer()
        card_buf.reset(is_continuation=is_continuation)

        # Clear overlay for a new question
        if session.overlay_viewers:
            await send_to_overlay(session, {"type": "card_clear"})

        # ── Groq fast-flash: fire in parallel with Claude ──
        t_flash_start = time.monotonic()
        if groq_available():
            context_summary = ""
            for doc in session.context_docs:
                if doc.get("doc_type") == "resume":
                    context_summary = doc.get("content", "")[:500]
                    break
            flash_task = asyncio.create_task(
                generate_groq_flash(
                    conversation=session.conversation,
                    last_interviewer_text=interviewer_text,
                    context_summary=context_summary,
                )
            )
        else:
            flash_task = None

        flash_sent = False

        # Build strategy context if strategy has been compiled
        strategy_ctx = None
        if session.strategy_brief:
            strategy_ctx = build_strategy_context(
                strategy_brief=session.strategy_brief,
                round_type=session.round_type,
                spoken_rules=session.spoken_rules,
                design_state=session.design_state,
                stories_told=session.stories_told,
                coding_state=session.coding_state,
            )

        full_text = ""
        async for chunk in generate_coaching(
            context_docs=session.context_docs,
            conversation=session.conversation,
            custom_prompt=session.custom_prompt,
            last_suggestion=prev_suggestion,
            candidate_progress=candidate_said,
            last_interviewer_text=interviewer_text,
            strategy_ctx=strategy_ctx,
            question_type=q_type,
            convo_state=session.convo_state,
        ):
            if not session.is_active:
                break

            if not full_text:
                t_first_token = time.monotonic()
                metrics._t_first_token = t_first_token

            # Before the first Claude chunk, try to send Groq flash
            if not flash_sent and flash_task is not None:
                if flash_task.done():
                    flash_text = flash_task.result()
                    t_groq_flash_done = time.monotonic()
                    if flash_text:
                        await send_to_dashboard(session, {
                            "type": "suggestion_flash",
                            "text": flash_text,
                        })
                    flash_sent = True
                elif not full_text:
                    try:
                        flash_text = await asyncio.wait_for(flash_task, timeout=0.3)
                        t_groq_flash_done = time.monotonic()
                        if flash_text:
                            await send_to_dashboard(session, {
                                "type": "suggestion_flash",
                                "text": flash_text,
                            })
                    except asyncio.TimeoutError:
                        pass
                    flash_sent = True

            full_text += chunk
            await send_to_dashboard(session, {
                "type": "suggestion_chunk",
                "text": chunk,
            })

            # Feed chunk to card buffer — stream cards in real-time
            actions = card_buf.feed(chunk)
            for action in actions:
                if action["action"] == "push":
                    # First real card — demote filler if one was sent
                    if metrics.filler_generated and metrics.total_cards == 0:
                        if session.overlay_viewers:
                            await send_to_overlay(session, {"type": "card_demote", "card_id": filler_id})
                        await send_to_dashboard(session, {"type": "card_demote", "card_id": filler_id})
                    if not metrics._t_first_card:
                        metrics._t_first_card = time.monotonic()
                    metrics.total_cards += 1
                    msg = card_to_message(action["card"])
                    await send_to_dashboard(session, msg)
                    if session.overlay_auto_relay and session.overlay_viewers:
                        await send_to_overlay(session, msg)
                        metrics.cards_auto_relayed += 1
                elif action["action"] == "update":
                    metrics.card_updates_sent += 1
                    msg = {"type": "card_update", "card_id": action["card_id"], "text": action["text"]}
                    await send_to_dashboard(session, msg)
                    if session.overlay_auto_relay and session.overlay_viewers:
                        await send_to_overlay(session, msg)
                elif action["action"] == "finalize":
                    msg = {"type": "card_update", "card_id": action["card_id"], "text": action["text"]}
                    await send_to_dashboard(session, msg)
                    if session.overlay_auto_relay and session.overlay_viewers:
                        await send_to_overlay(session, msg)

        # Cancel flash task if still running
        if flash_task and not flash_task.done():
            flash_task.cancel()

        # Finalize remaining buffer into cards
        final_actions = card_buf.finalize()
        for action in final_actions:
            if action["action"] == "push":
                msg = card_to_message(action["card"])
                await send_to_dashboard(session, msg)
                if session.overlay_auto_relay and session.overlay_viewers:
                    await send_to_overlay(session, msg)
            elif action["action"] in ("update", "finalize"):
                msg = {"type": "card_update", "card_id": action["card_id"], "text": action["text"]}
                await send_to_dashboard(session, msg)
                if session.overlay_auto_relay and session.overlay_viewers:
                    await send_to_overlay(session, msg)

        # Set total count on all cards retroactively via a summary message
        total_cards = len(card_buf.cards)
        await send_to_dashboard(session, {
            "type": "cards_complete",
            "total": total_cards,
            "card_ids": [c.card_id for c in card_buf.cards],
        })

        # Save suggestion to conversation history
        session.conversation.append({
            "role": "suggestion",
            "content": full_text,
        })

        # Update continuation state for the next cycle
        session.last_suggestion = full_text
        session.candidate_progress = ""
        session._candidate_current_turn = ""

        # Record answer metrics
        metrics.answer_text = full_text
        metrics.answer_word_count = len(full_text.split())

        # Update round-specific evolving state
        if session.round_type == "system_design":
            _update_design_state(session, full_text, interviewer_text)
        elif session.round_type == "behavioral":
            _update_behavioral_state(session, full_text)
        elif session.round_type == "technical_coding":
            _update_coding_state(session, full_text, interviewer_text)

        t_end = time.monotonic()
        await send_to_dashboard(session, {
            "type": "suggestion_end",
            "full_text": full_text,
            "is_continuation": is_continuation,
        })

        _report = {
            "type": "latency_report",
            "provider": provider_used,
            "question_type": q_type,
            "classify_ms": round((t_classify_done - t_start) * 1000),
            "ttft_ms": round((t_first_token - t_start) * 1000) if t_first_token else 0,
            "groq_flash_ms": round((t_groq_flash_done - t_start) * 1000) if t_groq_flash_done else 0,
            "total_ms": round((t_end - t_start) * 1000),
            "word_count": len(full_text.split()),
        }
        logger.info(f"[Dual] Latency: {_report}")
        await send_to_dashboard(session, _report)

        logger.info(
            f"[Dual] Suggestion generated ({len(full_text.split())} words, "
            f"continuation={is_continuation}) for session {session.session_id}"
        )

        # Persist AI generation to DB
        asyncio.create_task(_save_event(session.session_id, "ai_suggestion", full_text))

        # ── TTS: disabled in v2 (cards replace audio relay) ──
        # if session.tts_enabled and full_text and elevenlabs_available():
        #     if session.tts_auto:
        #         asyncio.create_task(_stream_tts_to_dashboard(session, full_text))

        # ── Deduct free generation if using free trial ──
        if session.user_id:
            check = await db.check_can_generate(session.user_id)
            if check["source"] == "free_trial":
                remaining = await db.use_free_generation(session.user_id)
                logger.info(
                    f"[Dual] Free generation used for {session.session_id}, "
                    f"{remaining} remaining"
                )
                await send_to_dashboard(session, {
                    "type": "free_trial_update",
                    "remaining": remaining,
                })

    except asyncio.CancelledError:
        logger.info("[Dual] Suggestion generation cancelled (new speech detected)")
        metrics.record_error("cancelled")
    except Exception as e:
        logger.error(f"[Dual] Suggestion generation error: {e}")
        metrics.record_error(str(e))
        await send_to_dashboard(session, {
            "type": "error",
            "message": f"Failed to generate suggestion: {str(e)}",
        })
    finally:
        session.generating_suggestion = False
        # Log metrics for this question
        metrics._t_generation_done = time.monotonic()
        metrics.provider = provider_used
        if hasattr(session, '_metrics'):
            session._metrics.finish_question()


async def start_interviewer_pipeline(session: DualSession) -> None:
    """Start Deepgram connection for interviewer (linear16/16kHz from Mac)."""
    try:
        session.interviewer_dg_ws = await connect_deepgram(
            encoding="linear16", sample_rate=16000
        )
        session.interviewer_recv_task = asyncio.create_task(
            dual_deepgram_receiver(session, session.interviewer_dg_ws, "interviewer")
        )
        # Start keepalive task
        session._interviewer_keepalive = asyncio.create_task(
            _deepgram_keepalive(session, "interviewer")
        )
        logger.info(f"Interviewer pipeline started for {session.session_id}")
    except Exception as e:
        logger.error(f"Failed to start interviewer pipeline: {e}")
        await send_to_dashboard(session, {
            "type": "error",
            "message": f"Interviewer Deepgram failed: {e}",
        })


async def start_candidate_pipeline(
    session: DualSession,
    encoding: str = "mulaw",
    sample_rate: int = 8000,
) -> None:
    """Start Deepgram connection for candidate audio.

    Default: mulaw/8kHz (Twilio). Pass linear16/16000 for direct mic capture.
    """
    try:
        session.candidate_dg_ws = await connect_deepgram(
            encoding=encoding, sample_rate=sample_rate
        )
        session.candidate_recv_task = asyncio.create_task(
            dual_deepgram_receiver(session, session.candidate_dg_ws, "candidate")
        )
        # Start keepalive task
        session._candidate_keepalive = asyncio.create_task(
            _deepgram_keepalive(session, "candidate")
        )
        logger.info(
            f"Candidate pipeline started for {session.session_id} "
            f"({encoding}/{sample_rate})"
        )
    except Exception as e:
        logger.error(f"Failed to start candidate pipeline: {e}")
        await send_to_dashboard(session, {
            "type": "error",
            "message": f"Candidate Deepgram failed: {e}",
        })


async def _deepgram_keepalive(session: DualSession, speaker: str) -> None:
    """
    Send silence bytes to Deepgram every 5s to prevent timeout disconnection.
    Also monitors the connection and auto-reconnects if it drops.

    Deepgram Flux closes after ~10-15s of no audio data.
    Sending 160 bytes of silence (10ms at 16kHz) keeps it alive.
    """
    KEEPALIVE_INTERVAL = 5  # seconds
    SILENCE_BYTES = b'\x00' * 320  # 10ms of silence at 16kHz PCM16
    RECONNECT_DELAY = 2

    while session.is_active:
        try:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if not session.is_active:
                break

            dg_ws = session.interviewer_dg_ws if speaker == "interviewer" else session.candidate_dg_ws

            if dg_ws is None or dg_ws.state.name != "OPEN":
                logger.warning(f"[Keepalive:{speaker}] Deepgram disconnected for {session.session_id}, reconnecting...")
                await asyncio.sleep(RECONNECT_DELAY)

                if not session.is_active:
                    break

                # Reconnect
                try:
                    new_ws = await connect_deepgram(encoding="linear16", sample_rate=16000)
                    if speaker == "interviewer":
                        session.interviewer_dg_ws = new_ws
                        if session.interviewer_recv_task:
                            session.interviewer_recv_task.cancel()
                        session.interviewer_recv_task = asyncio.create_task(
                            dual_deepgram_receiver(session, new_ws, "interviewer")
                        )
                    else:
                        session.candidate_dg_ws = new_ws
                        if session.candidate_recv_task:
                            session.candidate_recv_task.cancel()
                        session.candidate_recv_task = asyncio.create_task(
                            dual_deepgram_receiver(session, new_ws, "candidate")
                        )
                    logger.info(f"[Keepalive:{speaker}] Reconnected Deepgram for {session.session_id}")
                    await send_to_dashboard(session, {
                        "type": "status",
                        "speaker": speaker,
                        "status": "reconnected",
                        "message": f"{speaker} Deepgram reconnected after silence timeout",
                    })
                except Exception as e:
                    logger.error(f"[Keepalive:{speaker}] Reconnect failed: {e}")
                continue

            # Send keepalive silence
            try:
                await dg_ws.send(SILENCE_BYTES)
            except Exception:
                pass  # Will be caught on next loop iteration

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"[Keepalive:{speaker}] Error: {e}")
            await asyncio.sleep(RECONNECT_DELAY)


async def forward_mac_audio(session: DualSession, audio_bytes: bytes) -> None:
    """Forward raw PCM audio from Mac Electron to interviewer Deepgram."""
    if session.interviewer_dg_ws:
        try:
            if session.interviewer_dg_ws.state.name == "OPEN":
                await session.interviewer_dg_ws.send(audio_bytes)
        except Exception:
            pass


async def forward_mic_audio(session: DualSession, audio_bytes: bytes) -> None:
    """Forward raw PCM audio from Mac microphone to candidate Deepgram."""
    if session.candidate_dg_ws:
        try:
            if session.candidate_dg_ws.state.name == "OPEN":
                await session.candidate_dg_ws.send(audio_bytes)
        except Exception:
            pass


async def forward_twilio_audio_dual(session: DualSession, payload: str) -> None:
    """Forward Twilio base64 mulaw audio to candidate Deepgram."""
    if session.candidate_dg_ws:
        try:
            audio_bytes = base64.b64decode(payload)
            await session.candidate_dg_ws.send(audio_bytes)
        except Exception as e:
            logger.error(f"Failed to forward Twilio audio (dual): {e}")


# ---------------------------------------------------------------------------
# Evolving State Updates (per round type)
# ---------------------------------------------------------------------------

_DESIGN_PHASES = [
    "clarifying questions",
    "requirements",
    "back-of-envelope",
    "high-level architecture",
    "api design",
    "data model",
    "deep dive",
    "bottlenecks",
    "scaling",
    "summary",
]


def _update_design_state(session: Union[ActiveSession, DualSession], suggestion: str, interviewer_text: str) -> None:
    """Track system design progress: phases covered, whiteboard content, interviewer redirects."""
    lower = suggestion.lower()
    interviewer_lower = interviewer_text.lower()

    for phase in _DESIGN_PHASES:
        if phase in lower and phase not in session.design_state["phases_covered"]:
            session.design_state["phases_covered"].append(phase)
            session.design_state["current_phase"] = phase

    redirect_triggers = [
        "let's dig into", "let's dive into", "can you go deeper",
        "tell me more about", "what about", "let's focus on",
        "how would you handle", "let's talk about",
    ]
    for trigger in redirect_triggers:
        if trigger in interviewer_lower:
            topic = interviewer_text[interviewer_lower.index(trigger) + len(trigger):].strip()
            topic = topic[:80].rstrip("?.!")
            if topic:
                session.design_state["current_phase"] = f"deep dive: {topic}"
                if topic not in session.design_state.get("interviewer_reactions", []):
                    session.design_state["interviewer_reactions"].append(topic)
            break

    # Extract whiteboard content if marked
    if "[whiteboard]" in lower or "[say]" in lower:
        wb_start = lower.find("[whiteboard]")
        say_start = lower.find("[say]")
        if wb_start != -1 and say_start != -1 and say_start > wb_start:
            wb_content = suggestion[wb_start + len("[whiteboard]"):say_start].strip()
            if wb_content:
                if session.design_state["whiteboard_content"]:
                    session.design_state["whiteboard_content"] += "\n" + wb_content
                else:
                    session.design_state["whiteboard_content"] = wb_content

    logger.info(
        f"[DesignState] phases={session.design_state['phases_covered']}, "
        f"current={session.design_state['current_phase']}"
    )


def _update_behavioral_state(session: Union[ActiveSession, DualSession], suggestion: str) -> None:
    """Track which stories have been told to avoid repetition."""
    lower = suggestion.lower()
    story_markers = ["at ", "when i was at ", "during my time at ", "while working on "]
    for marker in story_markers:
        idx = lower.find(marker)
        if idx != -1:
            snippet = suggestion[idx:idx + 100].split(".")[0].strip()
            if snippet and snippet not in session.stories_told:
                session.stories_told.append(snippet)
                break


def _update_coding_state(session: Union[ActiveSession, DualSession], suggestion: str, interviewer_text: str) -> None:
    """Track coding interview phase progression."""
    lower = suggestion.lower()
    interviewer_lower = interviewer_text.lower()

    if any(w in lower for w in ["restate", "understand the problem", "let me make sure"]):
        session.coding_state["problem_understood"] = True

    if any(w in lower for w in ["brute force", "approach", "optimal", "algorithm"]):
        session.coding_state["approach_discussed"] = True

    if any(w in lower for w in ["implement", "code", "function", "def ", "class "]):
        session.coding_state["coding_started"] = True

    if any(w in lower for w in ["test", "edge case", "complexity", "time complexity"]):
        session.coding_state["testing_done"] = True


async def stop_dual_session(session: DualSession) -> None:
    """Clean up a dual session."""
    session.is_active = False

    for task in [session.interviewer_recv_task, session.candidate_recv_task]:
        if task and not task.done():
            task.cancel()

    for dg_ws in [session.interviewer_dg_ws, session.candidate_dg_ws]:
        if dg_ws:
            try:
                await dg_ws.send(json.dumps({"type": "CloseStream"}))
                await dg_ws.close()
            except Exception:
                pass

    if session.session_id in dual_sessions:
        del dual_sessions[session.session_id]

    logger.info(f"Dual session stopped: {session.session_id}")
