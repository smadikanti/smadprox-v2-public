# SmadProx v2 — Architecture Document

This document captures the full system architecture for SmadProx v2, incorporating all decisions and concerns raised during the design conversation on 2026-03-24. Exact phrasing from the original discussion is preserved in blockquotes so the intent behind each decision is traceable.

---

## Table of Contents

1. [Vision & Iterative Path](#1-vision--iterative-path)
2. [Current State (What Exists Today)](#2-current-state-what-exists-today)
3. [Existing Code Inventory](#3-existing-code-inventory)
4. [Target Architecture](#4-target-architecture)
5. [Single Electron App Design](#5-single-electron-app-design)
6. [Operator Dashboard](#6-operator-dashboard)
7. [Script Generation (Replacing Cursor)](#7-script-generation-replacing-cursor)
8. [Candidate Onboarding Flow](#8-candidate-onboarding-flow)
9. [Scrolling Feed Overlay](#9-scrolling-feed-overlay)
10. [Content Priority & Override Logic](#10-content-priority--override-logic)
11. [Filler Cards — Disappearing Bridge UX](#11-filler-cards--disappearing-bridge-ux)
12. [Predictive Question Understanding](#12-predictive-question-understanding)
13. [Live Card Updates for Blending](#13-live-card-updates-for-blending)
14. [Mac-Only Decision](#14-mac-only-decision)
15. [Build Phases (Condensed)](#15-build-phases-condensed)
16. [File Map — What Goes Where](#16-file-map--what-goes-where)
17. [Source Code Lineage](#17-source-code-lineage)

---

## 1. Vision & Iterative Path

> "Do you understand my goal? I am trying to build the system iteratively. I will still be behind the machine trying to answer everything, but eventually our goal is that the candidate ultimately installs a machine, maybe loads some credits onto their application, and then they will be able to run the app entirely."

**Phase A (now):** Operator (you) is behind every interview via Chrome Remote Desktop. The system assists you — captures audio automatically, generates answers, shows cards. You speak fillers, click cards, draw on the candidate's machine.

**Phase B (near-term):** Operator workload drops. Auto-relay handles most cards. Predictive engine eliminates filler speaking. You monitor and intervene on edge cases only.

**Phase C (eventual):** Candidate self-serves. Installs app, loads credits, runs interviews independently. You review analytics and improve the system. Revenue scales without your time.

> "During this process, I will be only using this with my candidates. We only want to have a basic ability: the candidate downloads an app, they will connect, and they will do it. We do not need to have a fancy web page or things like that. Maybe some kind of setup instructions, but other than that we do not need anything."

**Implication:** No consumer-facing web app. No marketing site. The Electron app IS the product. Setup instructions are built into the app's first-run wizard. The operator dashboard is a web page served by the FastAPI backend.

---

## 2. Current State (What Exists Today)

### How an interview works right now (corrected from operator's description)

> "Today candidate starts a NoJumper transcription from their end, I will be copying transcript from what their NoJumper link shows."

> "Meanwhile I copy and paste the transcript today, I speak so that my NoJumper device will be able to have some content relayed to candidate so that they will have content to talk, since I have experience already, anything I speak will be a valid filler, highly relevant filler, but I am multi tasking, where I am speaking, copy pasting entire transcript that happened until now so that Cursor will have full context of what we were speaking and Cursor generates answer, I will mentally parse answer, while I continue to speak from memory, may be blend into the answer from Cursor, while I copy the answer from Cursor and paste on ElevenLabs etc."

**Operator is simultaneously doing 6 things:**
1. Speaking live filler through their own device (from experience/memory)
2. Copying the full transcript from the candidate's NoJumper link
3. Pasting transcript into Cursor so it has full conversation context
4. Reading Cursor's generated answer while still speaking from memory
5. Blending — transitioning from spoken filler into the Cursor-generated answer mid-sentence
6. Copying Cursor's answer and pasting into ElevenLabs for TTS relay

**Bottlenecks:**
- Operator IS the transcription pipeline (manual copy-paste from NoJumper)
- Operator IS the bridge/filler (speaking live from memory while waiting for Cursor)
- Operator IS the context manager (pasting full transcript into Cursor each time)
- Operator IS the relay (copy answer -> ElevenLabs -> candidate)
- Operator IS multitasking across speaking, reading, copying, pasting, and blending — all in real time
- Candidate gets unstructured mixed content (operator voice + TTS + raw text)

---

## 3. Existing Code Inventory

### 3.1 over-phone-smadprox (FastAPI backend)

**Location:** `/Users/smad/Downloads/SmadSideProjectsM4Pro/smad-projects-brain/projects/smad-launch-wip/over-phone-smadprox/`

**What exists and is reusable:**

| Component | File | Status | What it does |
|-----------|------|--------|-------------|
| Deepgram dual pipeline | `app/pipeline.py` | DONE | Two independent Deepgram Flux v2 WebSocket connections — interviewer via `/ws/mac/{session_id}` and candidate via `/ws/mic/{session_id}`. `start_interviewer_pipeline()`, `start_candidate_pipeline()`, `forward_mac_audio()`, `forward_mic_audio()`. |
| DualSession state | `app/pipeline.py` | DONE | Full session state: `overlay_viewers`, `overlay_auto_relay`, `dashboard_viewers`, `conversation`, `context_docs`, `last_suggestion`, `candidate_progress`, `_candidate_current_turn`, continuation awareness. |
| Coaching engine | `app/coach.py` | DONE | Claude Sonnet streaming via `generate_coaching()`. Groq flash via `generate_groq_flash()` (~200ms). Groq quick answers via `generate_groq_quick_answer()`. Question classification via `classify_question()`. Round-type-specific prompts (system design, behavioral, coding). |
| Card splitter | `app/card_splitter.py` | DONE | `CardBuffer` class splits streaming text on paragraph breaks, `[WHITEBOARD]`/`[SAY]` markers, 80-word max. Outputs `Card` objects with `card_id`, `text`, `index`, `is_whiteboard`, `is_continuation`, `is_final`. |
| Overlay WebSocket | `app/main.py` | DONE | `/ws/overlay/{session_id}` endpoint. Receives: `card_push`, `card_update`, `card_clear`, `card_highlight`. Read-only for the candidate. |
| Dashboard WebSocket | `app/main.py` | DONE | `/ws/dashboard/{session_id}` endpoint. Handles: `relay_card`, `relay_all_cards`, `set_overlay_auto_relay`, `generate_now`, `set_context`, `set_mode`, `play_tts`, `simulate_question`, `answer_gaps`. |
| Strategy engine | `app/strategy.py` | DONE | `compile_strategy()` takes resume + JD + interviewer + culture doc -> produces `StrategyBrief` with seniority level, round type, compiled brief (~1500 tokens), spoken rules, gap questions. |
| Three-phase filler engine | `app/filler_engine.py` | DONE | Phase 1 (~0ms): pre-computed fillers. Phase 2 (~200ms): keyword-extracted bridge. Phase 3 (1.5-3s): full Claude suggestion. |
| ElevenLabs TTS | `app/tts.py` | DONE | `stream_tts()` with turbo_v2_5 model, 24kHz PCM. `_stream_tts_to_dashboard()` sends base64 audio chunks. |
| Supabase integration | `app/supabase_client.py` | DONE | Auth, contexts, documents, sessions, messages, credits. Full CRUD. |
| Credit metering | `app/pipeline.py` | DONE | Minute-based tracking, background metering loop, free trial + paid credits. |
| HumanProx dashboard | `static/humanprox.html` | DONE | Operator dashboard with transcript panel, coaching suggestion stream, card relay panel, context upload, strategy compiler, mode toggles. |
| Suggestion generation | `app/pipeline.py` | DONE | `generate_dual_suggestion()`: question classification -> Groq flash (parallel) -> Claude streaming -> card splitting -> auto-relay to overlay if enabled. Latency reporting. Credit deduction. |

### 3.2 reverse-engineer-littlebird/electron (Invisible overlay)

**Location:** `/Users/smad/Downloads/reverse-engineer-littlebird/electron/`

**What exists and is reusable:**

| Component | File | Status | What it does |
|-----------|------|--------|-------------|
| Invisible window | `main.js` | DONE | `transparent: true`, `frame: false`, `alwaysOnTop: true`, `setContentProtection(true)`, `setIgnoreMouseEvents(true, {forward: true})`, `visibleOnAllWorkspaces: true`, `skipTaskbar: true`. Default 480x750. |
| Card rendering | `renderer.js` | DONE | `createCardEl()`, `addCard()`, `updateCard()`, `highlightCard()`, `clearCards()`. Current card highlighted (blue border), previous cards dimmed (45% opacity). Whiteboard cards in yellow + monospace. |
| Overlay WebSocket | `renderer.js` | DONE | Connects to `/ws/overlay/{session_id}`. Handles `card_push`, `card_update`, `card_clear`, `card_highlight`. Auto-reconnect with exponential backoff (1s -> 16s). Ping every 25s. |
| Auto-scroll teleprompter | `renderer.js` | DONE | `requestAnimationFrame` loop. Speed 1-5 (10-60 px/s). Pauses on user scroll (1s timeout). Targets "current" card at 15% from top. Dynamic bottom spacer. |
| Keyboard shortcuts | `main.js` | DONE | `Cmd+Shift+H` (show/hide), `Cmd+Shift+L` (click-through toggle), `Cmd+Shift+P` (pause), `Cmd+Shift+S` (auto-scroll), `Cmd+Shift+1-5` (speed), opacity +/-, arrow nudge. |
| Deep link support | `main.js` | DONE | `noscreen://session/{id}?server={url}` protocol handler. |
| Card styling | `styles.css` | DONE | Dark theme. Current card: blue border + brighter bg. Previous: green border + 45% opacity. Whiteboard: yellow + monospace. `fadeIn` animation (180ms). |

### 3.3 over-phone-smadprox/electron-candidate (HumanProx audio sender)

**Location:** `/Users/smad/Downloads/SmadSideProjectsM4Pro/smad-projects-brain/projects/smad-launch-wip/over-phone-smadprox/electron-candidate/`

**What exists and is reusable:**

| Component | File | Status | What it does |
|-----------|------|--------|-------------|
| Shared audio pipeline | `shared/audio-capture.js` | DONE | `startAudioPipeline()`: AudioContext @ 16kHz, ScriptProcessor (512 buffer), float32->int16 conversion, 80ms chunks (1280 samples), binary WebSocket send. |
| Dual stream capture | `shared/audio-capture.js` | DONE | `connectAudioStreams()`: creates two `getUserMedia` streams (system + mic), two WebSocket connections (`/ws/mac/` + `/ws/mic/`), two independent audio pipelines. |
| BlackHole detection | `electron-sender/renderer.js` | DONE | Auto-detects BlackHole by label. Validates multi-output/aggregate devices. Warns if selected device may not capture system audio. |
| Device setup UI | `electron-candidate/index.html` | DONE | Device enumeration, dropdown selection, invite code entry, candidate context fields (resume, JD, company, interviewer, prep notes, culture values). |
| Registration flow | `electron-candidate/index.html` | DONE | Sends `{type: "register", name, invite_code, context}` on system audio WebSocket. Server validates invite code. |

### 3.4 live-scribe-scroll (NoJumper — reference only)

**Location:** `/Users/smad/Downloads/SmadSideProjectsM4Pro/smad-projects-brain/projects/smad-production-apps/live-scribe-scroll/`

**Reusable patterns (not code):**
- Word-count chunking logic (SharedViewer.tsx): chunk by word count, break at sentence boundaries when >= 75% of limit
- Auto-scroll speed mapping and `SCROLL_SPEED_MULTIPLIER` approach
- User scroll detection (wheel/touch events, 1s pause, auto-resume)

### 3.5 smadprox-cursor-executions (This repo — coaching knowledge base)

**Location:** `/Users/smad/smadprox-cursor-executions/`

**Reusable for script generation:**
- `elaborate-script-prompt.md` (326 lines) — full prompt template for generating script.md
- `.cursor/rules/interview-coach.mdc` (358 lines) — coaching methodology rules
- `.cursor/rules/coaching-lessons.mdc` (181 lines) — accumulated lessons from live sessions
- `culture-notes.md` (581 lines) — company values for 70+ companies
- `system-design-prompt.md` (148 lines) — expanded system design reference
- `live-coaching-prompt.md` (98 lines) — fast-model prompt for live interviews

---

## 4. Target Architecture

```
                     CANDIDATE MACHINE (Mac)
                    +-----------------------+
                    |  SmadProx Electron    |
                    |  (single app)         |
                    |                       |
                    |  [Setup Window]       |  <- first-run: BlackHole config, YouTube test
                    |  [Overlay Window]     |  <- during interview: invisible scrolling feed
                    |                       |
                    |  System Audio --------|----> /ws/mac/{candidate_id}
                    |  (BlackHole)          |
                    |  Microphone ----------|----> /ws/mic/{candidate_id}
                    |                       |
                    |  Overlay WS <---------|---- /ws/overlay/{candidate_id}
                    +-----------------------+
                              |
                              | WebSocket (PCM16 @ 16kHz)
                              |
                    +-----------------------+
                    |  CLOUD (Hetzner)      |
                    |  FastAPI Backend      |
                    |                       |
                    |  Deepgram Flux v2     |  <- dual transcription
                    |  Coaching Engine      |  <- Claude + Groq
                    |  Card Splitter        |  <- paragraph segmentation
                    |  Predictive Engine    |  <- partial-question matching
                    |  Filler Engine        |  <- three-phase bridge
                    |  Script Generator     |  <- Claude Opus via API (replaces Cursor)
                    |  Strategy Engine      |  <- resume + JD -> StrategyBrief
                    |  Supabase Client      |  <- auth, scripts, sessions, credits
                    +-----------------------+
                              |
                              | WebSocket + HTTP
                              |
                    +-----------------------+
                    |  OPERATOR MACHINE     |
                    |  (your laptop)        |
                    |                       |
                    |  Browser: Dashboard   |  <- humanprox.html (served by backend)
                    |    - Transcript view  |
                    |    - Card relay panel |
                    |    - Operator mic     |  <- you speak, candidate sees cards
                    |    - Script review    |
                    |                       |
                    |  Chrome Remote Desktop|  <- drawing, coding on candidate machine
                    +-----------------------+
                              |
                              | Supabase
                              |
                    +-----------------------+
                    |  SUBMISSION PORTAL    |
                    |  (Vercel)             |
                    |                       |
                    |  /submit              |  <- candidate enters resume, JD, etc.
                    |  /dashboard           |  <- candidate sees status
                    |                       |
                    |  On submit:           |
                    |  -> Supabase insert   |
                    |  -> Webhook triggers  |
                    |     script generation |
                    +-----------------------+
```

---

## 5. Single Electron App Design

> "Can we actually bind everything into the same Electron app so that candidate doesn't need to install multiple Electron apps? Basically, candidate will install some kind of an Electron app. It config asks them to configure the black hole, I mean black hole, and they configure it and, right before the interview, then hit Start."

> "We need to prompt instructions for them to open YouTube so that they play something and we clearly see what exactly is happening. It shows real-time feedback of the transcript coming through, and the entire page should be invisible so that the candidate will be able to see that the answer is being generated."

### Architecture: One app, two windows

```
main.js (Electron main process)
|
+-- setupWindow = new BrowserWindow({
|     width: 500, height: 600,
|     frame: true,              // normal visible window
|     show: true                // shown during setup + pre-interview
|   })
|   Loads: setup.html + setup-renderer.js
|
+-- overlayWindow = new BrowserWindow({
|     width: 480, height: 750,
|     frame: false,
|     transparent: true,
|     alwaysOnTop: true,        // 'screen-saver' level
|     setContentProtection: true,
|     setIgnoreMouseEvents: true, {forward: true},
|     visibleOnAllWorkspaces: true,
|     skipTaskbar: true,
|     show: false               // hidden until "Start Interview"
|   })
|   Loads: overlay.html + overlay-renderer.js
|
+-- IPC handlers:
|     "start-interview" -> hide setup, show overlay, start audio streams + overlay WS
|     "stop-interview"  -> stop audio, hide overlay, show setup
|
+-- Global keyboard shortcuts:
|     Cmd+Shift+H -> toggle overlay visibility
|     Cmd+Shift+S -> bring back setup window
|     Cmd+Shift+L -> toggle click-through
|     Cmd+Shift+P -> pause (disconnect WS)
|     Cmd+Shift+1-5 -> scroll speed
|     Cmd+Shift+- / Cmd+Shift+= -> opacity
|     Cmd+Shift+arrows -> nudge overlay position
|
+-- Audio streams (shared/audio-capture.js):
      System audio (BlackHole) -> /ws/mac/{candidate_id}
      Microphone -> /ws/mic/{candidate_id}
```

### Code lineage for the combined app

| Combined app file | Lifted from | What changes |
|---|---|---|
| `main.js` | `electron-candidate/main.js` (window setup) + `reverse-engineer-littlebird/electron/main.js` (overlay window config + keyboard shortcuts) | Merge into single main process managing two windows |
| `setup.html` + `setup-renderer.js` | `electron-candidate/index.html` (device setup, BlackHole detection, registration) + `electron-sender/renderer.js` (BlackHole validation, device enumeration) | Add first-run wizard (BlackHole install guide), YouTube test mode, candidate_id entry |
| `overlay.html` + `overlay-renderer.js` | `reverse-engineer-littlebird/electron/renderer.js` (card rendering, WebSocket, auto-scroll) + `styles.css` | Change from single-card highlight to scrolling feed. Add operator card type. Add filler card type with fade. |
| `shared/audio-capture.js` | `over-phone-smadprox/shared/audio-capture.js` | Use as-is. Already handles dual streams, PCM16 @ 16kHz, 80ms chunks. |
| `package.json` | New | Electron ^33.0.0, electron-store, electron-builder for .dmg packaging |

### First-run wizard flow

```
Step 1: Install BlackHole
  - Explains what BlackHole is and why it's needed
  - [Download BlackHole 2ch] button -> opens browser
  - [Open Audio MIDI Setup] button -> launches Audio MIDI Setup.app
  - Instructions: create Multi-Output Device (speakers + BlackHole)
  - [I've done this ->] button to proceed

Step 2: Select audio devices
  - System audio: dropdown (auto-selects BlackHole if detected)
  - Microphone: dropdown (auto-selects built-in mic)
  - Validation: warns if system device is not BlackHole/multi-output

Step 3: Test with YouTube
  - [Open YouTube test video] button
  - Captures system audio -> sends to Deepgram (temporary test session)
  - Shows real-time transcript in a text box
  - Checkmarks: "Hearing audio", "Transcript coming through"
  - [Looks good ->] button to proceed

Step 4: Enter candidate ID
  - Text field for candidate_id (received from submission portal)
  - App stores this in electron-store (remembered for future interviews)
  - [Save Setup] button
```

### Pre-interview screen (shown on subsequent launches)

```
- Candidate ID: [SMAD-A3X9K2] (editable, pre-filled from store)
- System audio: BlackHole 2ch [checkmark]
- Microphone: MacBook Pro Mic [checkmark]
- Server connected: [checkmark]
- Script loaded: [checkmark]  (verifies script exists in Supabase for this candidate)

- [ Start Interview ] button

- Keyboard shortcuts reference (small text at bottom)
```

---

## 6. Operator Dashboard

The operator dashboard is `humanprox.html` served by the FastAPI backend. Accessed in a browser on the operator's laptop.

### What exists today

The current `humanprox.html` already has:
- Session picker
- Live transcript panel (interviewer + candidate)
- Coaching suggestion stream
- Card relay (click to send)
- Context upload (resume, JD, culture doc)
- Strategy compiler + gap answering
- Mode toggles (TTS, auto-generation)

### What needs to be added

**1. Candidate picker (replaces session picker)**
- List of candidates with generated scripts (from Supabase)
- Click a candidate -> loads their context, script, Chrome Remote Desktop info
- Shows script status: generating / ready / reviewed

**2. Operator microphone capture**

> "I also want some kind of speech support on the dashboard portal that I'm using, right? I'll still use a dashboard on my laptop, and I'll be able to talk, and the candidate should be able to see."

- Mic capture button (or push-to-talk key)
- Operator speech -> Deepgram transcription -> displayed on dashboard
- Simultaneously sent as `operator_card` messages to `/ws/overlay/{candidate_id}`
- Candidate sees operator speech as distinct cards in the scrolling feed

**3. Script review panel**
- View the auto-generated script.md
- Edit inline if needed
- "Approve and Load" button to push to coaching engine context
- Re-generate button if script quality is poor

**4. Override mode indicator**
- Visual indicator showing whether AI or operator is currently driving the overlay
- When operator mic is active: "YOU ARE LIVE" indicator
- When AI is auto-relaying: "AI DRIVING" indicator

### Dashboard WebSocket additions

New message types from dashboard to backend:

```
operator_speak_start   -> overlay freezes AI cards, shows "operator incoming"
operator_card          -> {type: "operator_card", text: "...", is_final: false}
                          (streaming, sent on each transcription chunk)
operator_card_final    -> {type: "operator_card", text: "...", is_final: true}
                          (3s silence -> finalize card)
operator_speak_end     -> overlay resumes AI cards after 2s delay
```

Backend relays these to `/ws/overlay/{candidate_id}` for the candidate.

---

## 7. Script Generation (Replacing Cursor)

> "Where are we going to generate the script.md file? Because our cursor prompts are large, I'm still trying to understand how the workflow is going to look, because I'm no longer going to use cursor or open cursor on my machine. This has to be something like a web app. Everything happens on the backend after a candidate submits their script, and even if they edit their submission, we need to re-kick off and update our script.md, and that needs to be loaded into context."

### How it works

**Trigger:** Candidate creates or edits submission on `setup-instructions-smadprox.vercel.app/submit`

**Backend flow:**
1. Submission writes to Supabase `candidate_submissions` table
2. Supabase Database Webhook (or direct API call from portal) hits:
   `POST /api/generate-script/{submission_id}`
3. Endpoint reads submission data (resume, JD, interviewer LinkedIn, recruiter notes, round type)
4. Assembles prompt:
   - **System prompt:** content of `elaborate-script-prompt.md` (326 lines)
   - **Appended rules:** content of `interview-coach.mdc` (358 lines) + `coaching-lessons.mdc` (181 lines)
   - **Culture notes:** matched company section from `culture-notes.md` (if found)
   - **Prior rounds:** query Supabase for previous scripts for this candidate
   - **User message:** the submission data
5. Calls Claude Opus API: `anthropic.messages.create(model="claude-opus-4-6", ...)`
6. Stores generated script in Supabase `interview_scripts` table
7. Sends notification to operator dashboard

**On submission edit:** Same flow re-triggers. New script overwrites previous. Dashboard shows "Script regenerating..." status.

**Prompt size is not a problem.** The prompts are ~1,200 lines total. Claude Opus has 200K input context. This is well within limits.

### New Supabase tables

```sql
-- Script storage
CREATE TABLE interview_scripts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_id TEXT NOT NULL,           -- links to candidate_submissions
  submission_id UUID REFERENCES candidate_submissions(id),
  company TEXT,
  round_type TEXT,
  script_content TEXT,                  -- the generated script.md
  prompt_hash TEXT,                     -- hash of inputs for change detection
  status TEXT DEFAULT 'generating',     -- generating | ready | reviewed | error
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);
```

### New backend endpoint

```python
# app/main.py
@app.post("/api/generate-script/{submission_id}")
async def generate_script(submission_id: str):
    # 1. Read submission from Supabase
    # 2. Load prompt templates from disk (elaborate-script-prompt.md, rules, culture-notes)
    # 3. Match culture notes by company name
    # 4. Query prior scripts for this candidate
    # 5. Assemble full prompt
    # 6. Call Claude Opus API (streaming, store chunks)
    # 7. Store final script in interview_scripts table
    # 8. Notify dashboard via WebSocket
```

### Auto-load into coaching context

> "Maybe right before an interview, I am looking at this: I open something like a dashboard, I plug in a unique identifier that the candidate shares with me. The context is pre-loaded; the candidate is already wired into your system with audio and everything."

When the operator selects a candidate on the dashboard:
1. Dashboard sends `{type: "load_candidate", candidate_id: "SMAD-A3X9K2"}`
2. Backend queries Supabase for the latest `interview_scripts` where `candidate_id` matches
3. Loads `script_content` as `context_docs` on the `DualSession`
4. Runs `compile_strategy()` to produce `StrategyBrief`
5. Sends `{type: "context_ack", status: "ok", script_status: "ready"}` back to dashboard

When the candidate's Electron app connects with the same `candidate_id`:
- Audio streams are automatically associated with the session
- No manual session ID coordination needed

---

## 8. Candidate Onboarding Flow

> "We should also reduce the total number of things that the candidate needs to share with me so that we are aligned. Only Chrome Remote Desktop details will need to be shared so that I take access to his machine."

> "Once the candidate creates a submission, that will be the unique identifier creation workflow. Once a workflow is created, everything is loaded from my end from the candidate, and in the destination that candidate is going to stream their audio from their machine and things like that."

### What the candidate does (total)

**One-time onboarding (5-10 minutes):**
1. Go to submission portal, fill out: resume, JD, interviewer info, recruiter notes, Chrome Remote Desktop credentials
2. Submit -> receives `candidate_id` (e.g., `SMAD-A3X9K2`)
3. Download SmadProx Electron app (.dmg)
4. Install app, go through first-run wizard (BlackHole setup, YouTube test)
5. Enter `candidate_id` in the app (stored permanently)

**Before each interview (30 seconds):**
1. If new round: update submission on portal (new JD, interviewer) -> script auto-regenerates
2. Open SmadProx app (candidate_id remembered)
3. Hit "Start Interview"
4. Join Zoom/Teams/Meet

**What candidate shares with operator:** Chrome Remote Desktop credentials only (entered in submission form, stored in Supabase, visible on operator dashboard).

**What candidate never deals with:** Session IDs, server URLs, invite codes, multiple apps, NoJumper links, coordinating timing with operator.

### The `candidate_id` as master key

```
candidate_id: "SMAD-A3X9K2"
    |
    +-- candidate_submissions (resume, JD, interviewer, CRD creds)
    +-- interview_scripts (generated script.md, status)
    +-- sessions (audio streams, transcripts, coaching suggestions)
    +-- credits (balance, usage history)
```

Everything is keyed to `candidate_id`. When the Electron app connects with this ID, the backend knows exactly who they are and what context to load.

---

## 9. Scrolling Feed Overlay

> "And all of these cards have to be scrollable. It's not that one card appears and another card goes. It should be something like a conversation, like the no jumper is showing. The candidate should still have the flexibility of continuing a line that they are speaking if it is from the previous card."

### Behavior

The overlay is a **continuous scrolling feed**, not card-by-card replacement. Content flows downward. Everything stays readable. Nothing disappears abruptly.

- New cards append at the bottom of the feed
- Auto-scroll keeps the "current" card in the reading zone (~15% from top of overlay)
- Old cards scroll above but remain accessible — candidate can scroll up to reference earlier content
- Each card type has distinct visual styling (see Section 10)

### Why scrolling feed is better than single-card

| Single-card (rejected) | Scrolling feed (chosen) |
|---|---|
| Candidate loses previous context | Candidate can glance up to reference earlier points |
| Abrupt switch when new card replaces old | Smooth flow, new content appears below |
| If candidate is mid-sentence from card 1 and card 2 replaces it, they lose their place | Card 1 stays visible above, card 2 appears below — candidate finishes thought, eyes move down |
| Operator override replaces AI content | Operator content appears below AI content — both visible, candidate picks what to use |
| Filler disappearing = content jumping | Filler stays in feed but dims — no content jump, just visual de-emphasis |

### Auto-scroll behavior (lifted from existing NoScreen)

- `requestAnimationFrame` loop
- Speed 1-5 (10-60 px/s) controlled by `Cmd+Shift+1-5`
- Pauses on manual scroll (wheel/touch), resumes after 1s
- Targets "current" card at 15% from top of viewport
- Dynamic bottom spacer ensures last card can scroll to reading position

---

## 10. Content Priority & Override Logic

> "I also want some kind of speech support on the dashboard portal that I'm using. I'll still use a dashboard on my laptop, and I'll be able to talk, and the candidate should be able to see. When I'm doing that, I think we need to define and agree on a specific logic in terms of what needs to be overridden versus what is AI-generated."

### Priority hierarchy (highest to lowest)

```
1. OPERATOR LIVE (you speaking/typing)     <- always wins
2. OPERATOR CARD CLICK (you click a card)  <- overrides AI auto-relay
3. AI AUTO-RELAY (high confidence cards)   <- default when you're silent
4. AI FILLER (bridge cards)                <- lowest priority, first to disappear
```

### Override behavior in the scrolling feed

**Default state — AI is driving:**
- Interviewer asks question -> AI generates cards -> cards appear in overlay feed
- Cards auto-advance (auto-scroll keeps current card in reading zone)
- Operator is watching, not intervening

**Operator starts speaking — immediate override:**
- Operator presses hotkey on dashboard (or mic detects voice)
- AI cards in the feed **stop auto-scrolling** (freeze position)
- New operator card appears at the bottom of the feed (distinct orange border)
- Operator card grows in real-time as operator speaks (each transcription chunk appends)
- AI's queued-but-not-yet-displayed cards **dim** (lower opacity) — they're still there but visually secondary

**Operator stops speaking (3s silence) — handoff back to AI:**
- Operator card finalizes (cursor disappears, border goes solid)
- After 2s pause, AI cards resume auto-scrolling
- Next AI card's opening has been live-updated to bridge from what operator said (see Section 13)

**Operator clicks a specific AI card on dashboard:**
- That card pushes to overlay immediately, scrolls into reading position
- Skipped cards stay in feed but stay dimmed

**Operator is drawing on CRD + speaking:**
- Operator speech appears as orange cards in feed
- AI cards suppressed (dimmed, no auto-scroll) while operator is active
- When operator pauses drawing and stops talking -> AI resumes

### Visual card types in the overlay feed

```
AI card (auto-generated):
+-- [blue left border] ---- Current 1/3 --+
| The way I would approach this is by      |
| thinking about the data access patterns  |
| first. At my previous role we had        |
| roughly ten million daily queries...     |
+------------------------------------------+

Operator card (live, growing):
+-- [orange left border, pulsing] -- Live -+
| So let me draw this out, we have a       |
| client that talks to an API gateway ▌    |  <- blinking cursor
+------------------------------------------+

Operator card (finalized):
+-- [orange left border, solid] -----------+
| So let me draw this out, we have a       |
| client that talks to an API gateway,     |
| and behind that we have a queue for      |
| async processing.                        |
+------------------------------------------+

Filler card (bridge, will be superseded):
+-- [dashed border, lighter bg] -- Bridge -+
| Yeah so thinking about data pipelines,   |
| that's something I...                    |
|  [========-------] updating soon         |  <- progress indicator
|  "You can say this while we prepare"     |  <- small instruction
+------------------------------------------+

Previous card (already read):
+-- [dim, 40% opacity] ---- 1/3 ----------+
| The way I would approach this...         |
+------------------------------------------+
```

---

## 11. Filler Cards — Disappearing Bridge UX

> "Maybe let's say that while the interviewer is talking and we have captured some keywords, we can give some kind of a filler to the candidate, maybe a line or two lines. The candidate will only speak after the interviewer is done asking the entire question. By that time, we already generate an answer and we understand the question, so we need to kind of disappear that filler that we already gave to the candidate, right? The disappearing should happen very smoothly; we need to show by what time that is going to disappear."

> "Once it starts disappearing, a candidate can choose to not answer that, and the instructions should be clear to the candidate as well."

> "Maybe, after eventually they get to a point where they are habituated with the app, they'll be able to do that. Other than that, we can show instructions very small, mentioning you can say this while we generate an answer for you."

### Filler lifecycle in the scrolling feed

**While interviewer is talking (filler appears):**
1. Predictive engine detects keywords from partial transcript
2. Filler card appears at bottom of feed with:
   - Dashed border (visually distinct from real answer cards)
   - Small instruction text: *"You can say this while we prepare your answer"*
   - Progress indicator showing estimated time until real answer

**When full answer is ready (filler de-emphasizes):**
1. Filler card does NOT disappear (no content jumping in scrolling feed)
2. Instead: filler card smoothly transitions to 30% opacity over 300ms
3. Its label changes from "Bridge" to "(bridge — answer below)"
4. Real answer cards appear below it in the feed with full opacity
5. Auto-scroll advances past the dimmed filler to the first real answer card

**From the candidate's perspective:**
- They see a filler, optionally start speaking it
- Real answer appears below — they see it coming
- They naturally transition: finish their filler thought, eyes move down to the real answer
- No abrupt content change, no lost place, no jumping

### Filler card implementation

New card type in the overlay WebSocket protocol:

```json
{
  "type": "card_push",
  "card_id": "filler-abc123",
  "text": "Yeah so thinking about data pipelines, that's something I...",
  "is_filler": true,
  "instruction": "You can say this while we prepare your answer",
  "estimated_seconds": 3
}
```

When real answer arrives, backend sends:

```json
{
  "type": "card_demote",
  "card_id": "filler-abc123"
}
```

Overlay renderer handles `card_demote`: transitions the filler card to dimmed state (opacity, label change) over 300ms CSS transition.

---

## 12. Predictive Question Understanding

> "The main part that I'm thinking is we should be actually listening to the question and have a mental brain that is ready to understand the question. I think we need to iteratively identify what the interviewer is asking based off of the context that's already been there, like the transcript. As a new token comes through, we will try to determine what the question the interviewers are going to ask about, and depending on that we need to generate a highly relevant filler."

> "In the beginning, it's okay even if you are doing multiple trips. The ultimate goal is to reduce the latency and increase relevancy, and eventually we can optimize cost."

### Iterative classification as tokens arrive

The predictive engine is NOT a one-shot classifier. It refines its understanding continuously as the interviewer speaks.

**Token 1-5 (~first 1s):** Too early. Accumulate text.

**Token 5-15 (~1-3s):** Start matching against script.md sections.
- Keyword overlap: "tell me about a time" -> behavioral, "how would you design" -> system design, project name from resume -> project deep dive
- Send question-type signal to filler engine
- Filler engine generates a category-level bridge

**Token 15-30 (~3-6s):** Higher confidence.
- Match against specific anticipated Q&A pairs in script.md
- If >70% keyword match to a pre-written question -> pre-fetch that answer and start formatting as cards
- Generate a **specific filler** from the matched section: opening sentence of the pre-fetched answer, rephrased as a bridge

**EndOfTurn (interviewer stops):** Final validation.
- If pre-fetched answer matches -> push cards immediately (<500ms)
- If mismatch -> discard, generate fresh with Claude, push bridge filler while generating

### Filler relevancy improves with each token

```
After 1s:  generic filler ("Yeah, so...")
After 3s:  category filler ("That's a great question about system design, so...")
After 5s:  specific filler ("Yeah, so thinking about the data pipeline architecture...")
After EOT: either real answer (if predicted) or strong bridge (if generating)
```

### Cost is acceptable

Each Groq classification call is ~50ms and costs fractions of a cent. Running 3-4 classifications during a 10-second question is negligible cost. Optimize later; prioritize latency and relevancy now.

### Implementation

New module: `app/predictive.py`

```python
class PredictiveEngine:
    def __init__(self, context_docs: list[dict]):
        self.script_sections = parse_script_sections(context_docs)
        self.current_classification = None
        self.prefetched_cards = None
        self.confidence = 0.0

    async def on_partial_transcript(self, text: str, token_count: int):
        """Called on every Deepgram Update/StartOfTurn event."""
        if token_count < 5:
            return  # too early

        classification = await classify_with_groq(text)
        matched_section = self.match_script_section(classification, text)

        if matched_section and classification.confidence > 0.7:
            self.prefetched_cards = format_as_cards(matched_section)
            filler = generate_specific_filler(matched_section)
            return {"filler": filler, "confidence": classification.confidence}
        elif token_count > 10:
            filler = generate_category_filler(classification.question_type)
            return {"filler": filler, "confidence": classification.confidence}
        return None

    async def on_end_of_turn(self, full_text: str):
        """Called on Deepgram EndOfTurn. Returns pre-fetched cards or None."""
        if self.prefetched_cards and self.validate_prediction(full_text):
            cards = self.prefetched_cards
            self.reset()
            return cards  # Push immediately, <500ms
        self.reset()
        return None  # Generate normally
```

---

## 13. Live Card Updates for Blending

> "If that's the case, we should also live update the cards that we are showing to make sure that the candidate is blending in and not kind of abruptly stopping or starting something."

### The problem

If the candidate is mid-sentence reading card 1, and card 2 appears below with a completely different opening, there's a jarring context switch. The candidate has to mentally bridge from what they were saying to what card 2 starts with.

### The solution: continuation-aware card openings + live updates

**Card 2's opening is generated to flow from card 1.** The coaching engine already has continuation awareness (`last_suggestion` + `candidate_progress`). Claude is instructed to start each new card with a natural transition from the previous one.

**If the candidate deviates from the script,** the backend hears via candidate mic transcription. The next card's opening is re-generated to bridge from what the candidate actually said:

```
Card 1 said: "...ten million daily queries and the bottleneck
was always the analytics path."

Candidate actually said: "...ten million daily queries, and honestly
the interesting part was we also had a real-time dashboard that
needed sub-second updates."

Card 2 LIVE UPDATES from:
  "So what I did was partition by tenant ID..."
to:
  "And for that real-time dashboard piece, the approach
   I took was using change data capture with..."
```

Card 2's text changes in-place on the overlay via `card_update` WebSocket message. The `card_update` handler already exists in the NoScreen overlay renderer.

### Text transition UX

When a card updates its text, instead of snapping:

```css
.card-body {
  transition: opacity 150ms ease-out;
}
.card-body.updating {
  opacity: 0.4;    /* briefly dims during update */
}
/* after 150ms, new text set, class removed, opacity returns to 1.0 */
```

From the candidate's perspective: the card "shimmers" briefly and the opening changes. If they've already read past it, the update is invisible to them.

### Operator card growing in real-time

When operator is speaking, their card in the feed grows as speech is transcribed:

```
Frame 1 (operator starts):
+-- [orange, pulsing] -- Live --------+
| So let me ▌                         |
+-------------------------------------+

Frame 2 (500ms later):
+-- [orange, pulsing] -- Live --------+
| So let me draw this out, we ▌       |
+-------------------------------------+

Frame 3 (1s later):
+-- [orange, pulsing] -- Live --------+
| So let me draw this out, we have    |
| a client that talks to an API ▌     |
+-------------------------------------+

Frame 4 (operator pauses 3s, finalizes):
+-- [orange, solid] -- Operator ------+
| So let me draw this out, we have    |
| a client that talks to an API       |
| gateway, and behind that we have    |
| a queue for async processing.       |
+-------------------------------------+
```

The `▌` cursor disappears on finalize. Border stops pulsing. Auto-scroll resumes for AI cards.

---

## 14. Mac-Only Decision

> "I believe Windows is not going to work better, but please prove me or correct me if I'm wrong. Ultimately, I am thinking of candidates being able to use a Mac machine. I believe invisibility of the actual pane is going to be very crucial."

**Mac is correct. Windows is unreliable for this use case.**

| Feature | macOS | Windows |
|---|---|---|
| `setContentProtection(true)` | Rock solid — window is black/blank in all screen shares (Zoom, Teams, Meet, OBS) | Uses `SetWindowDisplayAffinity` — works on Win 10 2004+ but some screen recording tools and virtual cameras bypass it |
| `alwaysOnTop('screen-saver')` | Renders above everything including fullscreen apps | Fewer z-order levels; some fullscreen apps cover it |
| `setIgnoreMouseEvents(true, {forward: true})` | Clean click-through to app underneath | Edge cases with certain Windows UI frameworks |
| BlackHole virtual audio | Clean install, stable, well-maintained, free | Needs VB-Cable or similar — driver signing issues, less stable |
| Chrome Remote Desktop | Stable, well-tested | Works but virtual audio routing is harder |

**Decision:** Mac only. Don't spend time on Windows compatibility. Target audience is software engineers who predominantly use Macs. If a candidate only has Windows, that's a separate conversation for later.

---

## 15. Build Phases (Condensed)

The original 5 phases are condensed based on the design conversation. Script generation (originally Phase 4) is pulled into Phase A because the operator needs it to stop using Cursor.

### Phase A: Core System (Operator-Assisted)

**Goal:** Operator opens dashboard, selects candidate, speaks + clicks cards. No more Cursor, no more copy-paste, no more NoJumper transcript relay.

**What to build:**

1. **Combined Electron app** (new, lifts code from 3 repos)
   - Two-window architecture (setup + overlay)
   - First-run wizard with BlackHole guide and YouTube test
   - candidate_id entry and storage
   - Audio capture to backend (lift from `shared/audio-capture.js`)
   - Overlay with scrolling feed (lift from `reverse-engineer-littlebird/electron/`)
   - Package as .dmg

2. **Script generation endpoint** on backend (new)
   - `POST /api/generate-script/{submission_id}`
   - Assembles prompts from `elaborate-script-prompt.md` + rules + culture notes
   - Calls Claude Opus API
   - Stores in Supabase `interview_scripts` table
   - Triggered by submission portal webhook

3. **Dashboard enhancements** (modify existing `humanprox.html`)
   - Candidate picker (instead of session picker)
   - Auto-load context from Supabase when candidate selected
   - Operator mic capture -> transcription -> operator cards to overlay
   - Script review panel
   - Override mode indicator (AI driving vs operator live)

4. **Overlay card types** (extend existing card renderer)
   - AI cards (blue, existing)
   - Operator cards (orange, new — growing in real-time)
   - Filler cards (dashed, with instruction text and progress indicator)
   - Card demote (filler dims when real answer arrives)
   - Scrolling feed behavior (cards accumulate, don't replace)

5. **Backend wiring** (modify existing `main.py` + `pipeline.py`)
   - `candidate_id` as session key (instead of random UUID)
   - `load_candidate` message type on dashboard WS
   - `operator_speak_start`, `operator_card`, `operator_speak_end` relay to overlay
   - `card_demote` message type for filler de-emphasis
   - Supabase query for scripts on candidate load

**Result after Phase A:**
- You open dashboard, pick candidate, context auto-loads
- Candidate opens app, hits Start, audio streams automatically
- You connect Chrome Remote Desktop
- Interviewer speaks -> Deepgram transcribes -> coaching engine generates -> cards appear on your dashboard
- You click cards to relay OR speak and your words appear as orange cards
- Filler cards give candidate something to say while answer generates
- You draw/code on CRD while speaking, candidate sees your speech as cards
- No Cursor. No copy-paste. No NoJumper relay.

### Phase B: Automation (Reduce Operator Workload)

**Goal:** Most cards auto-relay. Predictive engine eliminates filler gap. Operator monitors and intervenes on edge cases only.

**What to build:**

1. **Confidence scoring** (new function in `coach.py`)
   - Score each generated answer's relevance (0-1) against the question via Groq
   - High confidence (>0.8) -> auto-relay to overlay
   - Low confidence (<0.8) -> hold on dashboard for operator review

2. **Predictive engine** (new `app/predictive.py`)
   - Iterative classification on partial Deepgram transcripts
   - Script section matching
   - Pre-fetched card formatting
   - Filler relevancy improvement as tokens arrive
   - Prediction validation on EndOfTurn

3. **Live card updates for blending** (modify `pipeline.py` + overlay renderer)
   - Continuation-aware card openings
   - Re-generate next card's opening when candidate deviates
   - `card_update` with cross-fade transition in overlay

4. **Smart card pacing** (modify `pipeline.py`)
   - Push first card immediately
   - Estimate reading time per card (~150 WPM)
   - Push next card when estimated reading time elapses

**Result after Phase B:**
- 70-80% of questions auto-relay without operator intervention
- Cards appear <500ms after interviewer stops (when predicted correctly)
- Filler cards are highly specific (matched to predicted question)
- Next card's opening blends with what candidate actually said
- Operator intervenes on edge cases only

### Phase C: Full Autonomy (Candidate Self-Serve)

**Goal:** Candidate installs app, loads credits, runs interviews independently.

**What to build:**

1. Credit system in Electron app (load credits, check balance)
2. Session auto-creation on app connect (no operator involvement)
3. Interruption handling (interviewer interrupts -> clear cards, generate pivot)
4. Repetition avoidance (track what candidate said, don't repeat)
5. Post-interview analytics dashboard
6. Candidate onboarding without operator (self-guided setup)

---

## 16. File Map — What Goes Where

### New project structure: `/Users/smad/smadprox-v2/`

```
smadprox-v2/
|
+-- ARCHITECTURE.md                    # This document
|
+-- electron/                          # Combined Electron app (candidate installs this)
|   +-- main.js                        # Main process: two windows, IPC, keyboard shortcuts
|   +-- setup.html                     # Setup window HTML
|   +-- setup-renderer.js              # Setup logic: device config, BlackHole, YouTube test
|   +-- overlay.html                   # Overlay window HTML
|   +-- overlay-renderer.js            # Card rendering, scrolling feed, WebSocket
|   +-- overlay-styles.css             # Card styling: AI, operator, filler, dimmed
|   +-- preload-setup.js               # IPC bridge for setup window
|   +-- preload-overlay.js             # IPC bridge for overlay window
|   +-- shared/
|   |   +-- audio-capture.js           # Lifted from over-phone-smadprox/shared/
|   +-- package.json                   # Electron + electron-builder
|   +-- build/
|       +-- entitlements.mac.plist     # Audio input permission
|       +-- icon.icns                  # App icon
|
+-- backend/                           # FastAPI backend (deployed to Hetzner)
|   +-- app/
|   |   +-- main.py                    # Lifted from over-phone-smadprox, modified
|   |   +-- pipeline.py                # Lifted, modified (candidate_id keying, operator relay)
|   |   +-- coach.py                   # Lifted, modified (confidence scoring)
|   |   +-- strategy.py                # Lifted as-is
|   |   +-- card_splitter.py           # Lifted, modified (filler card type, card_demote)
|   |   +-- filler_engine.py           # Lifted as-is
|   |   +-- predictive.py              # NEW: iterative question classification
|   |   +-- script_generator.py        # NEW: Claude Opus script generation
|   |   +-- tts.py                     # Lifted as-is
|   |   +-- supabase_client.py         # Lifted, modified (interview_scripts table)
|   |   +-- config.py                  # Lifted as-is
|   +-- static/
|   |   +-- humanprox.html             # Lifted, modified (candidate picker, operator mic, script review)
|   |   +-- style.css                  # Lifted, modified
|   |   +-- api.js                     # Lifted as-is
|   +-- prompts/                       # Prompt templates (lifted from cursor-executions repo)
|   |   +-- elaborate-script-prompt.md
|   |   +-- interview-coach-rules.md   # Content of interview-coach.mdc
|   |   +-- coaching-lessons.md        # Content of coaching-lessons.mdc
|   |   +-- culture-notes.md
|   |   +-- system-design-prompt.md
|   |   +-- live-coaching-prompt.md
|   +-- requirements.txt
|   +-- run.py
|   +-- .env.example
|
+-- docs/                              # Additional documentation
    +-- candidate-setup-guide.md       # For candidates: how to install and configure
    +-- operator-runbook.md            # For operator: how to use the dashboard
```

---

## 17. Source Code Lineage

Every file in smadprox-v2 traces back to an existing source. This table shows where each file comes from and what modifications are needed.

### Electron app

| New file | Source | Modifications |
|---|---|---|
| `electron/main.js` | `reverse-engineer-littlebird/electron/main.js` (overlay window config, keyboard shortcuts) + `over-phone-smadprox/electron-candidate/main.js` (window creation) | Merge into single main process. Two BrowserWindow instances. Add IPC for start/stop interview. Add `Cmd+Shift+S` to bring back setup window. |
| `electron/setup.html` | `over-phone-smadprox/electron-candidate/index.html` (device setup UI, BlackHole detection, context fields) | Add first-run wizard steps (BlackHole install guide, Audio MIDI Setup launcher, YouTube test). Replace invite code with candidate_id. Add script-loaded status check. |
| `electron/setup-renderer.js` | Extract JS from `electron-candidate/index.html` (inline script) + `electron-sender/renderer.js` lines 683-848 (BlackHole validation) | Add YouTube test mode (temporary Deepgram connection for audio verification). Add candidate_id persistence via electron-store. |
| `electron/overlay.html` | `reverse-engineer-littlebird/electron/index.html` | Minimal changes — already has card container + bottom bar structure. |
| `electron/overlay-renderer.js` | `reverse-engineer-littlebird/electron/renderer.js` | Add operator card type (orange, growing). Add filler card type (dashed, with instruction + progress). Add `card_demote` handler. Change from highlight-current to scrolling-feed (cards accumulate, old cards dim but stay). |
| `electron/overlay-styles.css` | `reverse-engineer-littlebird/electron/styles.css` | Add `.operator-card` styles (orange border, pulsing). Add `.filler-card` styles (dashed border, instruction text). Add `.card-demoted` styles (30% opacity transition). Add cross-fade for `card_update` text changes. |
| `electron/shared/audio-capture.js` | `over-phone-smadprox/shared/audio-capture.js` | Use as-is. Already handles dual streams, PCM16 @ 16kHz, 80ms chunks, float32->int16 conversion. |
| `electron/preload-setup.js` | `over-phone-smadprox/electron-candidate/preload.js` (if exists) or new | Expose IPC: `startInterview()`, `stopInterview()`, `getStoredConfig()`, `saveConfig()`. |
| `electron/preload-overlay.js` | `reverse-engineer-littlebird/electron/preload.js` | Expose IPC: `onSpeedChanged`, `onToggleAutoscroll`, `onTogglePause`, `onOpacityChanged`, `onInteractiveChanged`, `onSessionConfig`. |

### Backend

| New file | Source | Modifications |
|---|---|---|
| `backend/app/main.py` | `over-phone-smadprox/app/main.py` | Add `POST /api/generate-script/{submission_id}`. Add `load_candidate` dashboard message type. Add `operator_speak_start/card/end` relay to overlay. Change session keying from UUID to `candidate_id`. |
| `backend/app/pipeline.py` | `over-phone-smadprox/app/pipeline.py` | Add predictive engine hooks in `dual_deepgram_receiver()` (call `on_partial_transcript` on Update events). Add operator card relay logic. Add `card_demote` emission when filler is superseded. |
| `backend/app/coach.py` | `over-phone-smadprox/app/coach.py` | Add confidence scoring function (Groq call to rate answer relevance 0-1). Add continuation-aware card opening re-generation on candidate deviation. |
| `backend/app/strategy.py` | `over-phone-smadprox/app/strategy.py` | Use as-is. |
| `backend/app/card_splitter.py` | `over-phone-smadprox/app/card_splitter.py` | Add `is_filler` field to `Card` dataclass. Add `card_demote` message builder. |
| `backend/app/filler_engine.py` | `over-phone-smadprox/app/filler_engine.py` | Use as-is. Three-phase filler already built. |
| `backend/app/predictive.py` | NEW | `PredictiveEngine` class: iterative classification on partial Deepgram transcripts, script section matching, pre-fetch card formatting, filler relevancy improvement, prediction validation on EndOfTurn. |
| `backend/app/script_generator.py` | NEW | `generate_script()` function: reads submission from Supabase, loads prompt templates from `prompts/` directory, matches culture notes by company, queries prior scripts, calls Claude Opus API, stores result in Supabase. |
| `backend/app/tts.py` | `over-phone-smadprox/app/tts.py` | Use as-is. |
| `backend/app/supabase_client.py` | `over-phone-smadprox/app/supabase_client.py` | Add `interview_scripts` table operations: `create_script()`, `get_script_by_candidate()`, `update_script()`. |
| `backend/app/config.py` | `over-phone-smadprox/app/config.py` | Use as-is. |
| `backend/static/humanprox.html` | `over-phone-smadprox/static/humanprox.html` | Add candidate picker dropdown (replaces session picker). Add operator mic capture (getUserMedia + Deepgram in browser). Add script review panel. Add override mode indicator. Add operator speech -> WebSocket relay. |
| `backend/prompts/*` | `smadprox-cursor-executions/` | Lift content from: `elaborate-script-prompt.md`, `.cursor/rules/interview-coach.mdc`, `.cursor/rules/coaching-lessons.mdc`, `culture-notes.md`, `system-design-prompt.md`, `live-coaching-prompt.md`. |
| `backend/requirements.txt` | `over-phone-smadprox/requirements.txt` | Use as-is. All dependencies already present. |

---

## Appendix: WebSocket Message Protocol (Complete)

### Backend -> Overlay (candidate's Electron app)

```
card_push        {type, card_id, text, index, total, is_whiteboard, is_continuation, is_final, is_filler, instruction, estimated_seconds}
card_update      {type, card_id, text}
card_clear       {type}
card_highlight   {type, card_id}
card_demote      {type, card_id}                    <- NEW: filler de-emphasis
operator_card    {type, card_id, text, is_final}    <- NEW: operator speech
status           {type, connected, session_active}
pong             {type}
```

### Dashboard -> Backend

```
load_candidate           {type, candidate_id}                     <- NEW: load candidate context
set_context              {type, docs, metaprompt, strategy_brief}
set_mode                 {type, mode, tts_enabled, tts_auto, gen_auto}
generate_now             {type, question}
relay_card               {type, card}
relay_all_cards          {type, cards}
set_overlay_auto_relay   {type, enabled}
play_tts                 {type, text}
simulate_question        {type, question}
answer_gaps              {type, answers}
operator_speak_start     {type}                                   <- NEW
operator_card            {type, text, is_final}                   <- NEW
operator_speak_end       {type}                                   <- NEW
ping                     {type}
heartbeat                {type, elapsed_seconds}
```

### Backend -> Dashboard

```
transcript       {type, speaker, text, is_final, event}
suggestion_start {type, is_continuation, question}
suggestion_chunk {type, text}
suggestion_flash {type, text}
suggestion_end   {type, full_text, is_continuation}
cards_complete   {type, total, card_ids}
latency_report   {type, provider, ttft_ms, total_ms, ...}
status           {type, speaker, status}
candidate_context {type, context, candidate_name}
context_ack      {type, status, script_status}                    <- MODIFIED: add script_status
free_trial_update {type, remaining}
limit_reached    {type, message}
credit_warning   {type, remaining}
script_ready     {type, candidate_id, script_status}              <- NEW
```
