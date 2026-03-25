"""
NoHuman - Real-time Conversation Coach

Main FastAPI application.
Routes:
  - Auth (signup, login)
  - Context CRUD (projects + documents)
  - Session management
  - Twilio webhook (incoming call → media stream)
  - WebSocket: browser teleprompter connection
  - WebSocket: Twilio media stream
  - Static file serving
"""

import asyncio
import json
import logging
import random
import string
import time as _time
from pathlib import Path
from typing import Optional
from xml.etree.ElementTree import Element, SubElement, tostring

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import settings
from app import supabase_client as db
from app.pipeline import (
    ActiveSession,
    active_sessions,
    forward_browser_audio,
    forward_twilio_audio,
    start_session_pipeline,
    stop_session_pipeline,
    # Dual-source
    DualSession,
    dual_sessions,
    start_interviewer_pipeline,
    start_candidate_pipeline,
    forward_mac_audio,
    forward_mic_audio,
    forward_twilio_audio_dual,
    stop_dual_session,
    send_to_dashboard,
    send_to_overlay,
    generate_dual_suggestion,
    _stream_tts_to_dashboard,
)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("nohuman")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="NoHuman", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
STATIC_DIR = Path(__file__).parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

async def _get_user_from_token(request: Request) -> dict:
    """Extract and validate user from Authorization header."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid token")
    token = auth[7:]
    try:
        user = await db.get_user(token)
        return {**user, "_token": token}
    except Exception:
        raise HTTPException(401, "Invalid or expired token")


def _get_token(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    return auth[7:]


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------

class SignupRequest(BaseModel):
    email: str
    password: str
    name: str = ""


class LoginRequest(BaseModel):
    email: str
    password: str


class ContextCreate(BaseModel):
    title: str
    system_prompt: str = ""
    conversation_type: str = "general"


class ContextUpdate(BaseModel):
    title: Optional[str] = None
    system_prompt: Optional[str] = None
    conversation_type: Optional[str] = None


class DocumentCreate(BaseModel):
    title: str
    content: str
    doc_type: str = "notes"


class DocumentUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    doc_type: Optional[str] = None


class SessionCreate(BaseModel):
    context_id: int


# ---------------------------------------------------------------------------
# Auth Routes
# ---------------------------------------------------------------------------

@app.post("/api/auth/signup")
async def signup(req: SignupRequest):
    try:
        result = await db.signup(req.email, req.password, req.name)
        return result
    except Exception as e:
        raise HTTPException(400, str(e))


@app.post("/api/auth/login")
async def login(req: LoginRequest):
    try:
        result = await db.login(req.email, req.password)
        return result
    except Exception:
        raise HTTPException(401, "Invalid credentials")


@app.get("/api/auth/me")
async def get_me(request: Request):
    user = await _get_user_from_token(request)
    user.pop("_token", None)
    return user


# ---------------------------------------------------------------------------
# Context Routes (like Claude Projects)
# ---------------------------------------------------------------------------

@app.get("/api/contexts")
async def list_contexts(request: Request):
    user = await _get_user_from_token(request)
    token = user["_token"]
    return await db.list_contexts(token, user["id"])


@app.post("/api/contexts")
async def create_context(req: ContextCreate, request: Request):
    user = await _get_user_from_token(request)
    token = user["_token"]
    return await db.create_context(token, user["id"], req.model_dump())


@app.get("/api/contexts/{context_id}")
async def get_context(context_id: int, request: Request):
    user = await _get_user_from_token(request)
    token = user["_token"]
    ctx = await db.get_context(token, context_id)
    if not ctx:
        raise HTTPException(404, "Context not found")
    return ctx


@app.patch("/api/contexts/{context_id}")
async def update_context(context_id: int, req: ContextUpdate, request: Request):
    user = await _get_user_from_token(request)
    token = user["_token"]
    data = {k: v for k, v in req.model_dump().items() if v is not None}
    return await db.update_context(token, context_id, data)


@app.delete("/api/contexts/{context_id}")
async def delete_context(context_id: int, request: Request):
    user = await _get_user_from_token(request)
    token = user["_token"]
    await db.delete_context(token, context_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Document Routes (files within a context)
# ---------------------------------------------------------------------------

@app.get("/api/contexts/{context_id}/documents")
async def list_documents(context_id: int, request: Request):
    user = await _get_user_from_token(request)
    token = user["_token"]
    return await db.list_documents(token, context_id)


@app.post("/api/contexts/{context_id}/documents")
async def create_document(context_id: int, req: DocumentCreate, request: Request):
    user = await _get_user_from_token(request)
    token = user["_token"]
    return await db.create_document(token, context_id, req.model_dump())


@app.patch("/api/documents/{doc_id}")
async def update_document(doc_id: int, req: DocumentUpdate, request: Request):
    user = await _get_user_from_token(request)
    token = user["_token"]
    data = {k: v for k, v in req.model_dump().items() if v is not None}
    return await db.update_document(token, doc_id, data)


@app.delete("/api/documents/{doc_id}")
async def delete_document(doc_id: int, request: Request):
    user = await _get_user_from_token(request)
    token = user["_token"]
    await db.delete_document(token, doc_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Session Routes
# ---------------------------------------------------------------------------

@app.post("/api/sessions/legacy")
async def create_session_legacy(req: SessionCreate, request: Request):
    user = await _get_user_from_token(request)
    token = user["_token"]
    session = await db.create_session(token, user["id"], req.context_id)
    return session


# ---------------------------------------------------------------------------
# Twilio Webhook (incoming call)
# ---------------------------------------------------------------------------

@app.post("/twilio/voice")
async def twilio_voice_webhook(request: Request):
    """
    Twilio hits this when someone calls the number.
    Routes to dual-source if a DualSession exists, otherwise original path.
    """
    form = await request.form()
    call_sid = form.get("CallSid", "")

    # Check if there's an active DualSession → route to dual endpoint
    has_dual = any(s.is_active for s in dual_sessions.values())

    response = Element("Response")
    say = SubElement(response, "Say")
    connect = SubElement(response, "Connect")
    stream = SubElement(connect, "Stream")

    if has_dual:
        say.text = "Connected. Your audio is being captured."
        stream.set("url", f"wss://{request.url.hostname}/ws/twilio-dual/{call_sid}")
        logger.info(f"Twilio call {call_sid} → dual-source mode")
    else:
        say.text = "Connected. Your coach is listening."
        stream.set("url", f"wss://{request.url.hostname}/ws/twilio/{call_sid}")
        logger.info(f"Twilio call {call_sid} → single-source mode")

    xml = tostring(response, encoding="unicode")
    return Response(content=xml, media_type="application/xml")


# ---------------------------------------------------------------------------
# WebSocket: Browser Teleprompter
# ---------------------------------------------------------------------------

@app.websocket("/ws/session/{session_id}")
async def browser_websocket(websocket: WebSocket, session_id: str):
    """
    Browser connects here for the teleprompter display.
    Handles: auth, session binding, audio from browser mic, and
    receives suggestions + fillers to display.
    """
    await websocket.accept()

    session: Optional[ActiveSession] = None

    try:
        # Expect first message to be auth
        auth_msg = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        token = auth_msg.get("token", "")
        if not token:
            await websocket.send_json({"type": "error", "message": "No token"})
            await websocket.close()
            return

        # Validate token
        try:
            user = await db.get_user(token)
        except Exception:
            await websocket.send_json({"type": "error", "message": "Invalid token"})
            await websocket.close()
            return

        # Get session from DB
        db_session = await db.get_session(token, session_id)
        if not db_session:
            await websocket.send_json({"type": "error", "message": "Session not found"})
            await websocket.close()
            return

        # Load context + documents
        context = await db.get_context(token, db_session["context_id"])
        docs = await db.list_documents(token, db_session["context_id"])

        # Create active session
        session = ActiveSession(
            session_id=session_id,
            user_id=user["id"],
            context_id=db_session["context_id"],
            context_docs=docs,
            custom_prompt=context.get("system_prompt", "") if context else "",
            browser_ws=websocket,
        )
        active_sessions[session_id] = session

        # Compile strategy from context docs (same engine as HumanProx)
        try:
            from app.strategy import compile_strategy
            brief = await compile_strategy(docs)
            session.strategy_brief = brief.brief_text
            session.seniority_level = brief.seniority_level
            session.round_type = brief.round_type
            session.spoken_rules = brief.spoken_rules
            if brief.brief_text:
                logger.info(
                    f"[Browser] Strategy compiled for {session_id}: "
                    f"{brief.round_type} round, {brief.seniority_level} level"
                )
            if brief.gaps:
                await websocket.send_json({
                    "type": "strategy_gaps",
                    "gaps": [
                        {"id": g.id, "question": g.question, "options": g.options}
                        for g in brief.gaps
                    ],
                })
        except Exception as e:
            logger.warning(f"[Browser] Strategy compilation failed (using raw docs): {e}")

        # Start the audio pipeline (browser microphone mode)
        await start_session_pipeline(session, audio_source="browser")

        await websocket.send_json({
            "type": "status",
            "status": "ready",
            "message": "Coach is ready. Start talking!",
            "conversation_type": session.conversation_type,
        })

        # Update session status in DB
        await db.update_session(token, session_id, {"status": "active"})

        # Main message loop
        while True:
            message = await websocket.receive()

            if "bytes" in message:
                # Binary = audio data from browser mic
                await forward_browser_audio(session, message["bytes"])
            elif "text" in message:
                data = json.loads(message["text"])
                msg_type = data.get("type", "")

                if msg_type == "end_session":
                    break
                elif msg_type == "swap_speakers":
                    session.swap_speakers()
                elif msg_type == "i_am_speaking":
                    session.set_i_am_speaking()
                elif msg_type == "set_context":
                    new_docs = data.get("docs", [])
                    if new_docs:
                        session.context_docs = new_docs
                    meta = data.get("metaprompt", "")
                    if meta:
                        session.custom_prompt = meta
                    session.convo_state = {}
                    try:
                        from app.strategy import compile_strategy, StrategyBrief as SB
                        brief = await compile_strategy(
                            new_docs or session.context_docs,
                            round_type=data.get("round_type", ""),
                        )
                        session.strategy_brief = brief.brief_text
                        session.seniority_level = brief.seniority_level
                        session.round_type = brief.round_type
                        session.spoken_rules = brief.spoken_rules
                        logger.info(
                            f"[Browser] Strategy recompiled for {session_id}: "
                            f"{brief.round_type} round, {brief.seniority_level} level"
                        )
                        await websocket.send_json({"type": "context_ack", "status": "ok"})
                        if brief.gaps:
                            await websocket.send_json({
                                "type": "strategy_gaps",
                                "gaps": [
                                    {"id": g.id, "question": g.question, "options": g.options}
                                    for g in brief.gaps
                                ],
                            })
                    except Exception as e:
                        logger.warning(f"[Browser] Strategy recompile failed: {e}")
                        await websocket.send_json({"type": "context_ack", "status": "ok"})
                elif msg_type == "answer_gaps":
                    answers = data.get("answers", {})
                    if answers and session.strategy_brief:
                        from app.strategy import recompile_with_answers, StrategyBrief as SB
                        existing = SB(
                            seniority_level=session.seniority_level,
                            round_type=session.round_type,
                            brief_text=session.strategy_brief,
                            spoken_rules=session.spoken_rules,
                        )
                        await websocket.send_json({"type": "status", "message": "Refining strategy..."})
                        refined = await recompile_with_answers(
                            existing, answers, session.context_docs
                        )
                        session.strategy_brief = refined.brief_text
                        session.seniority_level = refined.seniority_level
                        session.round_type = refined.round_type
                        session.spoken_rules = refined.spoken_rules
                        await websocket.send_json({
                            "type": "context_ack",
                            "status": "ok",
                            "gaps_resolved": True,
                        })
                elif msg_type == "ping":
                    await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        logger.info(f"Browser disconnected from session {session_id}")
    except asyncio.TimeoutError:
        logger.warning(f"Browser auth timeout for session {session_id}")
    except Exception as e:
        logger.error(f"Browser WebSocket error: {e}")
    finally:
        if session:
            await stop_session_pipeline(session)
            try:
                if token:
                    await db.update_session(token, session_id, {
                        "status": "ended",
                        "ended_at": "now()",
                    })
            except Exception:
                pass


# ---------------------------------------------------------------------------
# WebSocket: Twilio MediaStream
# ---------------------------------------------------------------------------

@app.websocket("/ws/twilio/{call_sid}")
async def twilio_media_stream(websocket: WebSocket, call_sid: str):
    """
    Twilio connects here to stream call audio.
    We need to find the matching session and forward audio.
    """
    await websocket.accept()

    session: Optional[ActiveSession] = None

    try:
        # Find the active session waiting for this call
        for sid, s in active_sessions.items():
            if s.is_active and not s.twilio_ws:
                session = s
                session.twilio_ws = websocket
                break

        if not session:
            logger.warning(f"No active session for call {call_sid}")
            await websocket.close()
            return

        # If pipeline was started in browser mode, reconnect in Twilio mode
        await stop_session_pipeline(session)
        session.is_active = True
        session.browser_ws = session.browser_ws  # preserve browser connection
        active_sessions[session.session_id] = session
        await start_session_pipeline(session, audio_source="twilio")

        await send_to_browser_safe(session, {
            "type": "status",
            "status": "call_connected",
            "message": "Phone call connected. Listening...",
        })

        # Process Twilio media events
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            event = data.get("event", "")

            if event == "media":
                payload = data.get("media", {}).get("payload", "")
                if payload:
                    await forward_twilio_audio(session, payload)

            elif event == "stop":
                logger.info(f"Twilio stream stopped for call {call_sid}")
                break

    except WebSocketDisconnect:
        logger.info(f"Twilio disconnected for call {call_sid}")
    except Exception as e:
        logger.error(f"Twilio WebSocket error: {e}")


async def send_to_browser_safe(session: ActiveSession, message: dict) -> None:
    """Helper to send to browser with error handling."""
    if session.browser_ws:
        try:
            await session.browser_ws.send_json(message)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Dual-Source: Mac System Audio WebSocket
# ---------------------------------------------------------------------------

@app.websocket("/ws/mac/{session_id}")
async def mac_audio_websocket(websocket: WebSocket, session_id: str):
    """
    Friend's Electron app connects here to stream system audio (interviewer).
    No auth required. Sends raw PCM Int16 at 16kHz.
    """
    await websocket.accept()
    logger.info(f"[Dual] Mac connected for session {session_id}")

    # Create or reuse DualSession (resolve user_id from DB)
    if session_id not in dual_sessions:
        user_id = ""
        try:
            session_row = await db.get_session_by_id(session_id)
            if session_row:
                user_id = str(session_row.get("user_id", ""))
        except Exception as e:
            logger.warning(f"[Dual] Could not resolve user_id for {session_id}: {e}")
        import time as _time
        dual_sessions[session_id] = DualSession(
            session_id=session_id, user_id=user_id,
            connected_at=_time.time(),
        )
        # Start credit metering if user has paid credits
        if user_id:
            try:
                check = await db.check_can_generate(user_id)
                if check["source"] == "credits":
                    start_credit_metering(session_id, user_id)
            except Exception as e:
                logger.warning(f"[Dual] Could not start metering: {e}")
    session = dual_sessions[session_id]

    try:
        # Wait for either a register message (HumanProx) or raw audio (NoHuman).
        # HumanProx candidate app sends {"type":"register","invite_code":...}
        # NoHuman app sends binary audio frames immediately.
        first_audio_chunk = None
        registered = False
        while not registered:
            message = await websocket.receive()
            if "bytes" in message:
                # Raw audio arrived first — NoHuman flow, skip invite validation
                first_audio_chunk = message["bytes"]
                logger.info(f"[Dual] Mac stream started without invite (NoHuman) for {session_id}")
                registered = True
                break
            if "text" not in message:
                continue
            data = json.loads(message["text"])
            if data.get("type") != "register":
                continue

            code = data.get("invite_code", "").strip().upper()
            if not code or code not in invite_codes:
                logger.warning(f"[Dual] Invalid invite code '{code}' from session {session_id}")
                await websocket.send_json({
                    "type": "error",
                    "message": "Invalid invite code. Please check with your coach.",
                })
                await websocket.close(code=4001, reason="Invalid invite code")
                return
            inv = invite_codes[code]
            if inv["used"]:
                logger.warning(f"[Dual] Reused invite code '{code}' from session {session_id}")
                await websocket.send_json({
                    "type": "error",
                    "message": "This invite code has already been used.",
                })
                await websocket.close(code=4002, reason="Code already used")
                return
            inv["used"] = True
            inv["session_id"] = session_id
            logger.info(f"[Dual] Invite code {code} validated for session {session_id}")

            session.candidate_name = data.get("name", "")
            session.candidate_context = data.get("context", {})
            logger.info(
                f"[Dual] Candidate registered: "
                f"name={session.candidate_name}, sid={session_id}, "
                f"context_fields={list(k for k, v in session.candidate_context.items() if v)}"
            )
            if session.candidate_context:
                for viewer in session.dashboard_viewers:
                    try:
                        await viewer.send_json({
                            "type": "candidate_context",
                            "context": session.candidate_context,
                            "candidate_name": session.candidate_name,
                        })
                    except Exception:
                        pass
            registered = True

        # Start interviewer Deepgram pipeline
        await start_interviewer_pipeline(session)

        await websocket.send_json({
            "type": "status",
            "message": "Connected. Streaming interviewer audio to Deepgram.",
        })

        # Forward the first audio chunk that arrived before pipeline was ready
        if first_audio_chunk:
            await forward_mac_audio(session, first_audio_chunk)

        # Receive binary audio frames from Electron
        while True:
            message = await websocket.receive()
            if "bytes" in message:
                await forward_mac_audio(session, message["bytes"])
            elif "text" in message:
                data = json.loads(message["text"])
                if data.get("type") == "stop":
                    break

    except WebSocketDisconnect:
        logger.info(f"[Dual] Mac disconnected for session {session_id}")
    except Exception as e:
        logger.error(f"[Dual] Mac WebSocket error: {e}")
    finally:
        # Stop credit metering when Electron disconnects
        stop_credit_metering(session_id)
        # Stop interviewer pipeline but keep session alive for Twilio
        if session.interviewer_dg_ws:
            try:
                await session.interviewer_dg_ws.send(json.dumps({"type": "CloseStream"}))
                await session.interviewer_dg_ws.close()
            except Exception:
                pass
            session.interviewer_dg_ws = None
        if session.interviewer_recv_task and not session.interviewer_recv_task.done():
            session.interviewer_recv_task.cancel()


# ---------------------------------------------------------------------------
# Dual-Source: Microphone WebSocket (candidate audio from Electron)
# ---------------------------------------------------------------------------

@app.websocket("/ws/mic/{session_id}")
async def mic_audio_websocket(websocket: WebSocket, session_id: str):
    """
    Electron app connects here to stream microphone audio (candidate).
    No auth required. Sends raw PCM Int16 at 16kHz.
    """
    await websocket.accept()
    logger.info(f"[Dual] Mic connected for session {session_id}")

    # Create or reuse DualSession
    if session_id not in dual_sessions:
        dual_sessions[session_id] = DualSession(session_id=session_id)
    session = dual_sessions[session_id]

    try:
        # Start candidate Deepgram pipeline (linear16/16kHz for direct mic)
        await start_candidate_pipeline(
            session, encoding="linear16", sample_rate=16000
        )

        await websocket.send_json({
            "type": "status",
            "message": "Connected. Streaming candidate mic audio to Deepgram.",
        })

        await send_to_dashboard(session, {
            "type": "status",
            "speaker": "candidate",
            "status": "connected",
            "message": "Microphone connected. Listening for candidate.",
        })

        # Receive binary audio frames from Electron
        while True:
            message = await websocket.receive()
            if "bytes" in message:
                await forward_mic_audio(session, message["bytes"])
            elif "text" in message:
                data = json.loads(message["text"])
                if data.get("type") == "stop":
                    break

    except WebSocketDisconnect:
        logger.info(f"[Dual] Mic disconnected for session {session_id}")
    except Exception as e:
        logger.error(f"[Dual] Mic WebSocket error: {e}")
    finally:
        if session.candidate_dg_ws:
            try:
                await session.candidate_dg_ws.send(json.dumps({"type": "CloseStream"}))
                await session.candidate_dg_ws.close()
            except Exception:
                pass
            session.candidate_dg_ws = None
        if session.candidate_recv_task and not session.candidate_recv_task.done():
            session.candidate_recv_task.cancel()


# ---------------------------------------------------------------------------
# Dual-Source: Twilio Call → DualSession
# ---------------------------------------------------------------------------

@app.websocket("/ws/twilio-dual/{call_sid}")
async def twilio_dual_media_stream(websocket: WebSocket, call_sid: str):
    """
    Twilio connects here to stream candidate audio.
    Auto-links to the first active DualSession.
    """
    await websocket.accept()
    logger.info(f"[Dual] Twilio call connected: {call_sid}")

    session: Optional[DualSession] = None

    try:
        # Find an active DualSession to attach to
        for sid, s in dual_sessions.items():
            if s.is_active:
                session = s
                break

        if not session:
            # Create one if none exists
            session = DualSession(session_id="test")
            dual_sessions["test"] = session
            logger.info("[Dual] Created new DualSession for Twilio call")

        # Start candidate Deepgram pipeline
        await start_candidate_pipeline(session)
        logger.info(f"[Dual] Candidate pipeline attached to session {session.session_id}")

        await send_to_dashboard(session, {
            "type": "status",
            "speaker": "candidate",
            "status": "connected",
            "message": "Phone call connected. Listening for candidate.",
        })

        # Process Twilio media events
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            event_type = data.get("event", "")

            if event_type == "media":
                payload = data.get("media", {}).get("payload", "")
                if payload:
                    await forward_twilio_audio_dual(session, payload)
            elif event_type == "stop":
                logger.info(f"[Dual] Twilio stream stopped: {call_sid}")
                break

    except WebSocketDisconnect:
        logger.info(f"[Dual] Twilio disconnected: {call_sid}")
    except Exception as e:
        logger.error(f"[Dual] Twilio WebSocket error: {e}")
    finally:
        if session and session.candidate_dg_ws:
            try:
                await session.candidate_dg_ws.send(json.dumps({"type": "CloseStream"}))
                await session.candidate_dg_ws.close()
            except Exception:
                pass
            session.candidate_dg_ws = None
        if session and session.candidate_recv_task and not session.candidate_recv_task.done():
            session.candidate_recv_task.cancel()


# ---------------------------------------------------------------------------
# Dual-Source: Dashboard WebSocket (you watch transcripts here)
# ---------------------------------------------------------------------------

@app.websocket("/ws/dashboard/{session_id}")
async def dashboard_websocket(websocket: WebSocket, session_id: str):
    """
    Your browser connects here to receive dual transcripts in real-time.
    """
    await websocket.accept()
    logger.info(f"[Dashboard] Viewer connected for session {session_id}")

    # If old session exists but was stopped, replace it with a fresh one
    if session_id in dual_sessions:
        old = dual_sessions[session_id]
        if not old.is_active and not old.interviewer_dg_ws and not old.candidate_dg_ws:
            logger.info(f"[Dashboard] Replacing stale session {session_id}")
            del dual_sessions[session_id]

    # Create or reuse DualSession
    if session_id not in dual_sessions:
        dual_sessions[session_id] = DualSession(session_id=session_id)
    session = dual_sessions[session_id]
    session.dashboard_viewers.append(websocket)

    try:
        # Send current connection status for both pipelines
        if session.interviewer_dg_ws:
            await websocket.send_json({
                "type": "status",
                "speaker": "interviewer",
                "status": "connected",
                "message": "Interviewer pipeline active.",
            })
        if session.candidate_dg_ws:
            await websocket.send_json({
                "type": "status",
                "speaker": "candidate",
                "status": "connected",
                "message": "Candidate pipeline active.",
            })

        # Push candidate-provided context to dashboard
        if session.candidate_context:
            await websocket.send_json({
                "type": "candidate_context",
                "context": session.candidate_context,
                "candidate_name": session.candidate_name,
            })

        # Send current conversation history (only if session is active)
        if session.is_active and session.conversation:
            for entry in session.conversation:
                await websocket.send_json({
                    "type": "transcript",
                    "speaker": entry["role"],
                    "text": entry["content"],
                    "is_final": True,
                    "event": "EndOfTurn",
                    "turn_index": -1,
                })

        # Keep connection alive and handle commands
        while True:
            msg = await websocket.receive_text()
            data = json.loads(msg)

            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

            elif data.get("type") == "heartbeat":
                elapsed = data.get("elapsed_seconds", 0)
                minutes = round(elapsed / 60, 1)
                logger.info(
                    f"[Dashboard] Heartbeat for {session_id}: "
                    f"{minutes} min elapsed"
                )
                try:
                    sess_record = await db.get_session_by_id(session_id)
                    if sess_record:
                        await db.update_session_v2(
                            session_id, {"duration_minutes": minutes}
                        )
                except Exception as e:
                    logger.warning(f"[Dashboard] Heartbeat DB update failed: {e}")

            elif data.get("type") == "set_mode":
                session.mode = data.get("mode", "nohuman")
                session.tts_enabled = bool(data.get("tts_enabled", False))
                session.tts_voice_id = data.get("voice_id", "")
                session.tts_auto = bool(data.get("tts_auto", True))
                session.gen_auto = bool(data.get("gen_auto", True))
                logger.info(
                    f"[Dashboard] Mode set for {session_id}: "
                    f"mode={session.mode}, tts={session.tts_enabled}, "
                    f"tts_auto={session.tts_auto}, gen_auto={session.gen_auto}"
                )
                await websocket.send_json({
                    "type": "mode_ack",
                    "mode": session.mode,
                    "tts_enabled": session.tts_enabled,
                    "tts_auto": session.tts_auto,
                    "gen_auto": session.gen_auto,
                })

            elif data.get("type") == "generate_now":
                # Manual generation trigger from dashboard
                text = (
                    data.get("question")
                    or session._pending_interviewer_text
                    or session.last_interviewer_text
                )
                if text and len(text.split()) >= 3:
                    if session._suggestion_task and not session._suggestion_task.done():
                        session._suggestion_task.cancel()
                    session._suggestion_task = asyncio.create_task(
                        generate_dual_suggestion(session, text)
                    )
                    session._pending_interviewer_text = ""
                else:
                    await websocket.send_json({
                        "type": "error",
                        "message": "No interviewer question to generate from",
                    })

            elif data.get("type") == "play_tts":
                text = data.get("text", "")
                if text:
                    asyncio.create_task(
                        _stream_tts_to_dashboard(session, text)
                    )

            elif data.get("type") == "simulate_question":
                # Test mode: inject a fake interviewer question into the pipeline
                question = data.get("question", "")
                if question and len(question.split()) >= 3:
                    session.conversation.append({
                        "role": "interviewer",
                        "content": question,
                    })
                    session.last_interviewer_text = question
                    await send_to_dashboard(session, {
                        "type": "transcript",
                        "speaker": "interviewer",
                        "text": question,
                        "is_final": True,
                        "event": "EndOfTurn",
                        "turn_index": -1,
                    })
                    if session.gen_auto:
                        if session._suggestion_task and not session._suggestion_task.done():
                            session._suggestion_task.cancel()
                        session._suggestion_task = asyncio.create_task(
                            generate_dual_suggestion(session, question)
                        )
                    else:
                        session._pending_interviewer_text = question
                        await send_to_dashboard(session, {
                            "type": "generation_ready",
                            "question": question,
                        })

            elif data.get("type") == "set_context":
                session.context_docs = data.get("docs", [])
                session.custom_prompt = data.get("metaprompt", "")
                explicit_round = data.get("round_type", "")
                manual_brief = data.get("strategy_brief", "")
                doc_count = len(session.context_docs)
                logger.info(
                    f"[Dashboard] Context updated for {session_id}: "
                    f"{doc_count} docs, metaprompt={'yes' if session.custom_prompt else 'no'}, "
                    f"manual_brief={'yes' if manual_brief else 'no'}"
                )

                # Reset evolving state for new context
                session.design_state = {
                    "phases_covered": [],
                    "current_phase": "",
                    "whiteboard_content": "",
                    "interviewer_reactions": [],
                }
                session.stories_told = []
                session.coding_state = {
                    "problem_understood": False,
                    "approach_discussed": False,
                    "coding_started": False,
                    "testing_done": False,
                }
                session.convo_state = {}

                if manual_brief:
                    session.strategy_brief = manual_brief
                    session.round_type = explicit_round or "general"
                    session.seniority_level = "senior"
                    session.spoken_rules = ""
                    status_msg = f"Pre-compiled strategy loaded ({len(manual_brief)} chars). Round: {session.round_type}."
                    await websocket.send_json({
                        "type": "context_ack",
                        "message": status_msg,
                        "strategy_compiled": True,
                        "round_type": session.round_type,
                        "seniority_level": session.seniority_level,
                    })
                    logger.info(f"[Dashboard] {status_msg}")
                else:
                    await websocket.send_json({
                        "type": "context_ack",
                        "message": f"Context saved: {doc_count} documents. Compiling strategy...",
                    })

                    from app.strategy import compile_strategy
                    try:
                        brief = await compile_strategy(
                            session.context_docs,
                            explicit_round_type=explicit_round,
                        )
                        session.strategy_brief = brief.brief_text
                        session.seniority_level = brief.seniority_level
                        session.round_type = brief.round_type
                        session.spoken_rules = brief.spoken_rules

                        status_msg = (
                            f"Strategy compiled: {brief.round_type} round, "
                            f"{brief.seniority_level} level"
                        )
                        if not brief.brief_text:
                            status_msg = f"Context saved ({doc_count} docs). No strategy compiled (need resume or JD)."

                        await websocket.send_json({
                            "type": "context_ack",
                            "message": status_msg,
                            "strategy_compiled": bool(brief.brief_text),
                            "round_type": brief.round_type,
                            "seniority_level": brief.seniority_level,
                        })

                        # Send gap questions for operator refinement
                        if brief.gaps:
                            await websocket.send_json({
                                "type": "strategy_gaps",
                                "gaps": [
                                    {
                                        "id": g.id,
                                        "question": g.question,
                                        "options": g.options,
                                    }
                                    for g in brief.gaps
                                ],
                            })
                            logger.info(
                                f"[Dashboard] Sent {len(brief.gaps)} gap questions "
                                f"for {session_id}"
                            )

                        logger.info(f"[Dashboard] {status_msg}")
                    except Exception as e:
                        logger.error(f"[Dashboard] Strategy compilation failed: {e}")
                        await websocket.send_json({
                            "type": "context_ack",
                            "message": f"Context saved ({doc_count} docs). Strategy compilation failed — using raw docs.",
                        })

            elif data.get("type") == "answer_gaps":
                # Operator answered gap questions — recompile strategy
                answers = data.get("answers", {})
                if answers and session.strategy_brief:
                    from app.strategy import recompile_with_answers, StrategyBrief as SB
                    existing = SB(
                        seniority_level=session.seniority_level,
                        round_type=session.round_type,
                        brief_text=session.strategy_brief,
                        spoken_rules=session.spoken_rules,
                    )
                    await websocket.send_json({
                        "type": "context_ack",
                        "message": "Refining strategy with your answers...",
                    })
                    new_brief = await recompile_with_answers(
                        existing, answers, session.context_docs
                    )
                    session.strategy_brief = new_brief.brief_text
                    session.seniority_level = new_brief.seniority_level
                    session.round_type = new_brief.round_type
                    session.spoken_rules = new_brief.spoken_rules
                    await websocket.send_json({
                        "type": "context_ack",
                        "message": (
                            f"Strategy refined: {new_brief.round_type} round, "
                            f"{new_brief.seniority_level} level"
                        ),
                        "strategy_compiled": True,
                        "round_type": new_brief.round_type,
                        "seniority_level": new_brief.seniority_level,
                        "gaps_resolved": True,
                    })
                    logger.info(
                        f"[Dashboard] Strategy recompiled with {len(answers)} "
                        f"gap answers for {session_id}"
                    )

            # ─── v2: Load candidate by ID ─────────────────────────
            elif data.get("type") == "load_candidate":
                candidate_id = data.get("candidate_id", "").strip()
                if not candidate_id:
                    await websocket.send_json({
                        "type": "error",
                        "message": "candidate_id is required",
                    })
                else:
                    logger.info(f"[Dashboard] Loading candidate {candidate_id}")
                    try:
                        # Fetch latest script from Supabase
                        script_row = await db.get_latest_script(candidate_id)
                        if script_row and script_row.get("script_content"):
                            # Load script as a context document
                            session.context_docs = [{
                                "doc_type": "script",
                                "name": "script.md",
                                "content": script_row["script_content"],
                            }]
                            # Compile strategy from the loaded script
                            from app.strategy import compile_strategy
                            brief = await compile_strategy(
                                session.context_docs,
                                explicit_round_type=script_row.get("round_type", ""),
                            )
                            session.strategy_brief = brief.brief_text
                            session.seniority_level = brief.seniority_level
                            session.round_type = brief.round_type
                            session.spoken_rules = brief.spoken_rules

                            await websocket.send_json({
                                "type": "context_ack",
                                "status": "ok",
                                "script_status": script_row.get("status", "ready"),
                                "message": f"Loaded script for {candidate_id} ({brief.round_type}, {brief.seniority_level})",
                                "strategy_compiled": True,
                                "round_type": brief.round_type,
                                "seniority_level": brief.seniority_level,
                            })
                        else:
                            await websocket.send_json({
                                "type": "context_ack",
                                "status": "no_script",
                                "script_status": "missing",
                                "message": f"No script found for candidate {candidate_id}",
                            })
                    except Exception as e:
                        logger.error(f"[Dashboard] load_candidate error: {e}")
                        await websocket.send_json({
                            "type": "error",
                            "message": f"Failed to load candidate: {e}",
                        })

            # ─── v2: Operator speech relay to overlay ─────────────
            elif data.get("type") == "operator_speak_start":
                # Freeze AI cards on overlay, operator is live
                await send_to_overlay(session, {"type": "operator_speak_start"})
                logger.info(f"[Dashboard] Operator speaking for {session_id}")

            elif data.get("type") == "operator_card":
                # Relay operator speech as a card to overlay
                card_msg = {
                    "type": "operator_card",
                    "card_id": data.get("card_id", ""),
                    "text": data.get("text", ""),
                    "is_final": data.get("is_final", False),
                    "is_operator": True,
                }
                await send_to_overlay(session, card_msg)

            elif data.get("type") == "operator_speak_end":
                # Resume AI cards on overlay
                await send_to_overlay(session, {"type": "operator_speak_end"})
                logger.info(f"[Dashboard] Operator stopped speaking for {session_id}")

            elif data.get("type") == "relay_card":
                card_msg = data.get("card", {})
                if card_msg and session.overlay_viewers:
                    card_msg["type"] = "card_push"
                    await send_to_overlay(session, card_msg)
                    logger.info(
                        f"[Dashboard] Relayed card {card_msg.get('card_id', '?')} "
                        f"to {len(session.overlay_viewers)} overlay(s)"
                    )

            elif data.get("type") == "relay_all_cards":
                cards = data.get("cards", [])
                if cards and session.overlay_viewers:
                    await send_to_overlay(session, {"type": "card_clear"})
                    for card_msg in cards:
                        card_msg["type"] = "card_push"
                        await send_to_overlay(session, card_msg)
                        await asyncio.sleep(0.15)
                    logger.info(
                        f"[Dashboard] Relayed {len(cards)} cards to overlay"
                    )

            elif data.get("type") == "set_overlay_auto_relay":
                session.overlay_auto_relay = bool(data.get("enabled", False))
                logger.info(
                    f"[Dashboard] Overlay auto-relay: {session.overlay_auto_relay}"
                )
                await websocket.send_json({
                    "type": "overlay_auto_relay_ack",
                    "enabled": session.overlay_auto_relay,
                })

    except WebSocketDisconnect:
        logger.info(f"[Dashboard] Viewer disconnected for session {session_id}")
    except Exception:
        pass
    finally:
        if websocket in session.dashboard_viewers:
            session.dashboard_viewers.remove(websocket)


# ---------------------------------------------------------------------------
# Overlay WebSocket: Candidate NoScreen App
# ---------------------------------------------------------------------------

@app.websocket("/ws/overlay/{session_id}")
async def overlay_websocket(websocket: WebSocket, session_id: str):
    """
    Candidate's NoScreen overlay app connects here to receive coaching cards.
    Read-only: no audio, no context — just card_push/card_clear/card_highlight.
    """
    await websocket.accept()
    logger.info(f"[Overlay] Connected for session {session_id}")

    if session_id not in dual_sessions:
        dual_sessions[session_id] = DualSession(session_id=session_id)
    session = dual_sessions[session_id]
    session.overlay_viewers.append(websocket)

    try:
        await websocket.send_json({
            "type": "status",
            "connected": True,
            "session_active": session.is_active,
            "session_id": session_id,
            "has_dashboard": len(session.dashboard_viewers) > 0,
        })

        while True:
            msg = await websocket.receive_text()
            data = json.loads(msg)

            if data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        logger.info(f"[Overlay] Disconnected for session {session_id}")
    except Exception:
        pass
    finally:
        if websocket in session.overlay_viewers:
            session.overlay_viewers.remove(websocket)


# ---------------------------------------------------------------------------
# Update Twilio Voice Webhook for Dual Mode
# ---------------------------------------------------------------------------

@app.post("/twilio/voice-dual")
async def twilio_voice_dual_webhook(request: Request):
    """
    Twilio hits this for dual-source mode.
    Streams to /ws/twilio-dual/ instead of /ws/twilio/.
    """
    form = await request.form()
    call_sid = form.get("CallSid", "")

    response = Element("Response")
    say = SubElement(response, "Say")
    say.text = "Connected. Your audio is being captured."
    connect = SubElement(response, "Connect")
    stream = SubElement(connect, "Stream")
    stream.set("url", f"wss://{request.url.hostname}/ws/twilio-dual/{call_sid}")

    xml = tostring(response, encoding="unicode")
    return Response(content=xml, media_type="application/xml")


# ---------------------------------------------------------------------------
# HTML Pages (served from static/)
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC_DIR / "index.html").read_text()


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    return (STATIC_DIR / "dashboard.html").read_text()


@app.get("/context/{context_id}", response_class=HTMLResponse)
async def context_page(context_id: int):
    return (STATIC_DIR / "context.html").read_text()


@app.get("/session/{session_id}", response_class=HTMLResponse)
async def session_page(session_id: str):
    return (STATIC_DIR / "teleprompter.html").read_text()


@app.get("/live", response_class=HTMLResponse)
async def live_dashboard(s: Optional[str] = None):
    html = (STATIC_DIR / "live.html").read_text()
    if s:
        html = html.replace(
            "const SESSION_ID = 'test';",
            f"const SESSION_ID = '{s}';",
        )
    return html


@app.get("/humanprox", response_class=HTMLResponse)
async def humanprox_dashboard():
    from starlette.responses import HTMLResponse as _HTML
    content = (STATIC_DIR / "humanprox.html").read_text()
    return _HTML(
        content=content,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


# ---------------------------------------------------------------------------
# HumanProx Invite Codes
# ---------------------------------------------------------------------------

invite_codes: dict[str, dict] = {}


def _gen_code() -> str:
    chars = string.ascii_uppercase + string.digits
    while True:
        code = ''.join(random.choices(chars, k=6))
        if code not in invite_codes:
            return code


class InviteRequest(BaseModel):
    name: str


@app.post("/api/hp/invite")
async def create_invite(req: InviteRequest):
    code = _gen_code()
    invite_codes[code] = {
        "candidate_name": req.name,
        "created_at": _time.time(),
        "used": False,
        "session_id": None,
    }
    logger.info(f"[Invite] Created code {code} for {req.name}")
    return {"code": code, "candidate_name": req.name}


@app.get("/api/hp/invites")
async def list_invites():
    result = []
    for code, info in invite_codes.items():
        result.append({
            "code": code,
            "candidate_name": info["candidate_name"],
            "created_at": info["created_at"],
            "used": info["used"],
            "session_id": info["session_id"],
        })
    result.sort(key=lambda x: x["created_at"], reverse=True)
    return {"invites": result}


@app.delete("/api/hp/invite/{code}")
async def delete_invite(code: str):
    if code in invite_codes:
        del invite_codes[code]
        return {"deleted": True}
    raise HTTPException(status_code=404, detail="Code not found")


@app.get("/api/hp/sessions")
async def list_humanprox_sessions():
    """List active DualSessions for the HumanProx session picker."""
    sessions = []
    for sid, s in dual_sessions.items():
        if not s.is_active:
            continue
        elapsed = int(_time.time() - s.connected_at) if s.connected_at else 0
        sessions.append({
            "session_id": sid,
            "candidate_name": s.candidate_name or "Unknown",
            "has_interviewer": s.interviewer_dg_ws is not None,
            "has_candidate": s.candidate_dg_ws is not None,
            "has_dashboard": len(s.dashboard_viewers) > 0,
            "elapsed_seconds": elapsed,
            "mode": s.mode,
        })
    return {"sessions": sessions}


# ---------------------------------------------------------------------------
# Health Check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "deepgram": bool(settings.DEEPGRAM_API_KEY),
        "anthropic": bool(settings.ANTHROPIC_API_KEY),
        "twilio": bool(settings.TWILIO_ACCOUNT_SID),
        "supabase": bool(settings.SUPABASE_URL),
    }


@app.get("/api/config/deepgram-key")
async def get_deepgram_key():
    """Serve Deepgram API key to dashboard for operator mic transcription."""
    return {"key": settings.DEEPGRAM_API_KEY}


# ---------------------------------------------------------------------------
# SaaS API: Clerk-authenticated endpoints
# ---------------------------------------------------------------------------

from app.clerk_auth import (  # noqa: E402
    get_clerk_user_id,
    get_user_profile_from_any_token,
    create_desktop_token,
)
from app import supabase_client as db_v2  # noqa: E402


async def _get_clerk_profile(request: Request) -> dict:
    """Extract JWT from Authorization header, verify (Clerk or desktop), return profile."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid token")
    token = auth_header[7:]

    profile = await get_user_profile_from_any_token(token)
    if not profile:
        raise HTTPException(401, "Invalid token or profile not found")
    return profile


@app.post("/api/v2/auth/desktop-token")
async def exchange_desktop_token(request: Request):
    """Exchange a short-lived Clerk JWT for a long-lived desktop token."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(401, "Missing or invalid token")
    token = auth_header[7:]

    clerk_user_id = await get_clerk_user_id(token)
    if not clerk_user_id:
        raise HTTPException(401, "Invalid Clerk token")

    desktop_token = create_desktop_token(clerk_user_id)
    return {"token": desktop_token}


# --- Credits ---

@app.get("/api/credits")
async def get_credits(request: Request):
    """Get the current user's credit balance and free trial status."""
    profile = await _get_clerk_profile(request)
    credits = await db.get_credits(profile["id"])
    return {
        "balance_minutes": credits["balance_minutes"] if credits else 0,
        "free_generations_remaining": credits.get("free_generations_remaining", 3) if credits else 3,
        "user_id": profile["id"],
    }


@app.post("/api/credits/check")
async def check_credits(request: Request):
    """Check if the user can generate (free trial or paid credits)."""
    profile = await _get_clerk_profile(request)
    result = await db.check_can_generate(profile["id"])
    credits = await db.get_credits(profile["id"])
    return {
        "allowed": result["allowed"],
        "reason": result["reason"],
        "source": result["source"],
        "balance_minutes": credits["balance_minutes"] if credits else 0,
        "free_generations_remaining": credits.get("free_generations_remaining", 3) if credits else 3,
    }


# --- Dev-only: Seed Credits ---

class SeedCreditsRequest(BaseModel):
    minutes: float = 60.0


@app.post("/api/dev/seed-credits")
async def seed_credits(req: SeedCreditsRequest, request: Request):
    """DEV ONLY: Add credits to the current user's account (bypasses Stripe)."""
    profile = await _get_clerk_profile(request)
    user_id = profile["id"]
    new_balance = await db.add_credits(user_id, req.minutes)
    return {
        "added_minutes": req.minutes,
        "new_balance": new_balance,
    }


# --- Sessions (SaaS v2) ---

class SessionCreateV2(BaseModel):
    resume_text: str = ""
    job_description: str = ""
    company_name: str = ""
    round_type: str = "general"
    visa_status: str = ""
    recruiter_notes: str = ""
    context_id: Optional[int] = None


@app.post("/api/v2/sessions")
async def create_session_v2_endpoint(req: SessionCreateV2, request: Request):
    """Create a new interview session with context info."""
    profile = await _get_clerk_profile(request)
    session = await db.create_session_v2(
        user_id=profile["id"],
        context_id=req.context_id,
        data=req.model_dump(),
    )
    return {"session_id": session["id"], "session": session}


@app.get("/api/v2/sessions")
async def list_sessions(request: Request):
    """List all sessions for the current user."""
    profile = await _get_clerk_profile(request)
    sessions = await db.list_user_sessions(profile["id"])
    return sessions


@app.get("/api/v2/sessions/{session_id}")
async def get_session_detail(session_id: str, request: Request):
    """Get session detail with documents."""
    profile = await _get_clerk_profile(request)
    session = await db.get_session_by_id(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    if session.get("user_id") != profile["id"]:
        raise HTTPException(403, "Not your session")
    return session


@app.get("/api/v2/sessions/{session_id}/events")
async def get_session_events(session_id: str, request: Request):
    """Get all saved events (transcripts + AI suggestions) for a session."""
    profile = await _get_clerk_profile(request)
    session = await db.get_session_by_id(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    if session.get("user_id") != profile["id"]:
        raise HTTPException(403, "Not your session")
    events = await db_v2.get_session_messages(session_id)
    return {"session_id": session_id, "events": events}


class SendLinkRequest(BaseModel):
    phone: str


@app.post("/api/v2/sessions/{session_id}/send-link")
async def send_session_link(session_id: str, req: SendLinkRequest, request: Request):
    """Send the teleprompter link to the user's phone via SMS."""
    profile = await _get_clerk_profile(request)
    session = await db.get_session_by_id(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    if session.get("user_id") != profile["id"]:
        raise HTTPException(403, "Not your session")

    if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        raise HTTPException(503, "SMS service not configured")

    from twilio.rest import Client as TwilioClient  # noqa: E402

    teleprompter_url = f"{settings.PUBLIC_URL}/session/{session_id}"
    company = session.get("company_name") or "your interview"
    sms_body = (
        f"Your NoHuman live session for {company}: "
        f"{teleprompter_url}\n\n"
        f"Open this link on your phone for real-time coaching."
    )

    try:
        client = TwilioClient(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        client.messages.create(
            body=sms_body,
            from_=settings.TWILIO_PHONE_NUMBER,
            to=req.phone,
        )
    except Exception as exc:
        logger.error(f"Twilio SMS error: {exc}")
        raise HTTPException(502, "Failed to send SMS")

    return {"status": "sent"}


class SessionUpdateV2(BaseModel):
    company_name: Optional[str] = None
    round_type: Optional[str] = None
    recruiter_notes: Optional[str] = None
    visa_status: Optional[str] = None
    resume_text: Optional[str] = None
    job_description: Optional[str] = None


@app.patch("/api/v2/sessions/{session_id}")
async def update_session_v2_endpoint(
    session_id: str, req: SessionUpdateV2, request: Request
):
    """Update an existing session (owner only)."""
    profile = await _get_clerk_profile(request)
    session = await db.get_session_by_id(session_id)
    if not session:
        raise HTTPException(404, "Session not found")
    if session.get("user_id") != profile["id"]:
        raise HTTPException(403, "Not your session")
    update_data = {k: v for k, v in req.model_dump().items() if v is not None}
    if not update_data:
        return session
    updated = await db.update_session_v2(session_id, update_data)
    return updated


# ---------------------------------------------------------------------------
# Credit Metering Background Task
# ---------------------------------------------------------------------------

_metering_tasks: dict[str, asyncio.Task] = {}


async def credit_metering_loop(
    session_id: str, user_id: str, interval: int = 60
):
    """
    Background task that deducts 1 minute of credits every `interval` seconds.
    Sends warnings at 5 min, 2 min, and 0 min remaining.
    """
    try:
        while True:
            await asyncio.sleep(interval)

            # Deduct 1 minute
            updated = await db.decrement_credits(user_id, 1.0)
            await db.record_credit_usage(user_id, session_id, 1.0)

            if not updated:
                break

            balance = updated.get("balance_minutes", 0)
            logger.info(
                f"[Metering] Session {session_id}: deducted 1 min, "
                f"remaining={balance}"
            )

            # Update session duration
            session = await db.get_session_by_id(session_id)
            current_duration = session.get("duration_minutes", 0) or 0
            await db.update_session_v2(
                session_id, {"duration_minutes": current_duration + 1}
            )

            # Send warnings to dashboard
            if session_id in dual_sessions:
                ds = dual_sessions[session_id]
                if balance <= 5 and balance > 2:
                    await send_to_dashboard(ds, {
                        "type": "credit_warning",
                        "balance_minutes": balance,
                        "message": f"Warning: {balance:.0f} minutes remaining",
                    })
                elif balance <= 2 and balance > 0:
                    await send_to_dashboard(ds, {
                        "type": "credit_warning",
                        "balance_minutes": balance,
                        "message": f"Critical: {balance:.0f} minutes remaining!",
                    })
                elif balance <= 0:
                    await send_to_dashboard(ds, {
                        "type": "credit_exhausted",
                        "message": "Credits exhausted. Disconnecting in 30 seconds.",
                    })
                    # Grace period
                    await asyncio.sleep(30)
                    # Stop the session
                    if session_id in dual_sessions:
                        await stop_dual_session(dual_sessions[session_id])
                    break

    except asyncio.CancelledError:
        logger.info(f"[Metering] Task cancelled for session {session_id}")
    except Exception as e:
        logger.error(f"[Metering] Error for session {session_id}: {e}")


def start_credit_metering(session_id: str, user_id: str):
    """Start the credit metering background task for a session."""
    if session_id in _metering_tasks:
        _metering_tasks[session_id].cancel()
    task = asyncio.create_task(credit_metering_loop(session_id, user_id))
    _metering_tasks[session_id] = task
    logger.info(f"[Metering] Started for session {session_id}")


def stop_credit_metering(session_id: str):
    """Stop the credit metering for a session."""
    task = _metering_tasks.pop(session_id, None)
    if task and not task.done():
        task.cancel()
        logger.info(f"[Metering] Stopped for session {session_id}")


# ---------------------------------------------------------------------------
# v2: Script Generation (replaces Cursor)
# ---------------------------------------------------------------------------

class GenerateScriptRequest(BaseModel):
    submission_id: str


@app.post("/api/generate-script/{submission_id}")
async def generate_script_endpoint(submission_id: str):
    """
    Generate script.md from a candidate submission.
    Triggered by submission portal webhook or manual dashboard action.
    """
    from app.script_generator import generate_and_store_script

    logger.info(f"[ScriptGen] Starting generation for submission {submission_id}")

    try:
        result = await generate_and_store_script(
            supabase_client=db,
            submission_id=submission_id,
            on_status=lambda msg: logger.info(f"[ScriptGen] {msg}"),
        )

        # Notify any connected dashboards
        candidate_id = result.get("candidate_id", "")
        for sid, session in dual_sessions.items():
            if session.dashboard_viewers:
                await send_to_dashboard(session, {
                    "type": "script_ready",
                    "candidate_id": candidate_id,
                    "script_status": "ready",
                })

        logger.info(
            f"[ScriptGen] Script generated for {candidate_id}: "
            f"script_id={result.get('script_id')}"
        )
        return result

    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        logger.error(f"[ScriptGen] Generation failed: {e}")
        raise HTTPException(500, f"Script generation failed: {e}")


@app.get("/api/candidates")
async def list_candidates():
    """List all candidates with generated scripts."""
    try:
        candidates = await db.list_candidates_with_scripts()
        return {"candidates": candidates}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/generate-script-async/{submission_id}")
async def generate_script_async_endpoint(submission_id: str):
    """
    Same as above but runs in background. Returns immediately.
    """
    from app.script_generator import generate_and_store_script

    async def _run():
        try:
            result = await generate_and_store_script(
                supabase_client=db,
                submission_id=submission_id,
                on_status=lambda msg: logger.info(f"[ScriptGen] {msg}"),
            )
            candidate_id = result.get("candidate_id", "")
            for sid, session in dual_sessions.items():
                if session.dashboard_viewers:
                    await send_to_dashboard(session, {
                        "type": "script_ready",
                        "candidate_id": candidate_id,
                        "script_status": "ready",
                    })
        except Exception as e:
            logger.error(f"[ScriptGen] Async generation failed: {e}")

    asyncio.create_task(_run())
    return {"status": "generating", "submission_id": submission_id}
