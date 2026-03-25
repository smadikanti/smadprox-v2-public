# Amar Test 1 — Five9 Recruiter Screen — Post-Session Review

**Date:** 2026-03-25
**Session ID:** Amar123
**Candidate:** Amarendar Reddy
**Company:** Five9 — Sr. SDE, Professional Services
**Interviewer:** Rennie Nastor (Sr. TA Partner, 16 yrs recruiting)
**Duration:** ~48 minutes
**Round Type:** Recruiter screen

---

## What Worked

1. **Filler delivery: 182ms avg** — within 200ms target. Candidate always had something to say while full answer generated.
2. **Transcript capture** — both interviewer and candidate audio captured cleanly via loopback. No BlackHole needed.
3. **Electron app installed remotely** — friend installed .dmg, connected to ngrok-exposed backend, audio streamed across the internet.
4. **Script.md generated in Cursor** — 478-line prep script covering all angles. Loaded as context manually.
5. **Card streaming** — first card appears immediately as Claude starts generating, text grows in real-time.
6. **Answers were relevant** — coaching output matched the Cursor-generated script content (intro, why Five9, BT experience, comp deflection).

## What Broke

### 1. Prompt Caching: 0% Hit Rate
- **Expected:** Q2+ should read from cache (~350ms TTFT, 90% cheaper)
- **Actual:** Every question paid full price (~1,337ms TTFT, $0.15/question)
- **Root cause:** The `cache_control={"type": "ephemeral"}` parameter may not be supported in the streaming API's current SDK version, or the system prompt structure changes between calls (conversation history appended to messages breaks the cache prefix).
- **Impact:** $4.35 spent on 29 questions instead of ~$0.60 with caching. TTFT 4x slower than target.

### 2. Cards Rapidly Appearing and Disappearing
- **What happened:** The candidate saw answers flash by too quickly. Couldn't read one card before the next appeared.
- **Root cause:** Deepgram's aggressive endpointing split the interviewer's speech into many fragments, each triggering a new coaching generation. The new generation cleared the overlay and pushed new cards, replacing what the candidate was reading.
- **Fix needed:** Don't clear overlay on every new question. Only clear when the topic changes significantly. Keep all cards scrollable.

### 3. Compensation Question — Card Not Pushed
- **What happened:** Rennie asked about comp expectations. Coaching answer generated on operator dashboard but the card wasn't pushed to the candidate's overlay.
- **Root cause:** Auto-relay may have been off, or the generation was cancelled before cards could be relayed.
- **Fix needed:** Log relay success/failure per card. If auto-relay is on and overlay is connected, every card should push.

### 4. 143 False Questions Detected
- **What happened:** Out of 172 "questions" tracked, only 29 had coaching generated. The other 143 were speech fragments that Deepgram flagged as EndOfTurn but were too short to be real questions.
- **Root cause:** `endpointing=300` (300ms silence = end of turn) is too aggressive for natural conversation. Rennie pauses mid-sentence frequently.
- **Fix needed:** Increase to `endpointing=1000` or `1500`. Only trigger coaching when the interviewer utterance is >15 words.

### 5. No Time Awareness
- **What happened:** At minute 40, the coaching engine didn't know the call was almost over. It kept generating full answers instead of suggesting a close.
- **Fix needed:** Pass elapsed time to the coaching prompt. After 25 minutes, suggest wrapping. After 35 minutes, generate closing statements.

### 6. Haiku Routing Never Triggered
- **What happened:** All 26 generated answers used Sonnet. Zero used Haiku.
- **Root cause:** The question classifier didn't classify any questions as "follow_up" or "quick_answer" — likely because the partial question text was too short or didn't match keyword patterns.
- **Fix needed:** Tune classification to handle real interview speech patterns, not just textbook phrasing.

### 7. No Quality Feedback Collected
- **What happened:** The feedback bar (quality 1-5) was available on the dashboard but wasn't used during the session.
- **Root cause:** Too busy managing CRD, Cursor, NoJumper, and the dashboard simultaneously. No time to click quality buttons.
- **Fix needed:** Make feedback easier — keyboard shortcut (1-5 keys) instead of clicking buttons. Or auto-rate based on whether the operator overrode.

---

## Metrics Summary

| Metric | Value | Target | Gap |
|---|---|---|---|
| Total questions detected | 172 | — | 143 were false positives |
| Questions with coaching | 29 | — | — |
| Avg TTFT | 1,337ms | <500ms | 2.7x over target |
| Avg TTFC (first card) | 1,369ms | <500ms | 2.7x over target |
| Avg filler delivery | 182ms | <200ms | On target |
| Avg total generation | 3,346ms | <3,000ms | 12% over |
| Cache hits | 0/29 (0%) | >80% | Completely broken |
| Haiku routing | 0/29 | ~30% for follow-ups | Not triggered |
| Cards generated | 39 | — | — |
| Cards auto-relayed | 38 | — | 1 missed |
| Card updates (streaming) | 118 | — | Good streaming behavior |
| Operator overrides | 0 | — | — |
| Quality scores recorded | 0 | — | Feedback bar not used |
| Errors | 3 cancelled | 0 | Acceptable |
| Model | 100% Sonnet | — | No Haiku routing |
| Est. API cost | ~$4.35 | ~$0.60 with cache | 7x over with caching |

---

## Candidate Performance Notes (from Cursor agent chat)

- **Intro:** Covered key points but heavy on filler ("basically," "you know"). Content was right — BT experience, Southwest platform, why PS role. Could be tighter.
- **External customer question:** Good pivot to BT. Explained the client relationship well.
- **H-1B disclosure:** Revealed contractor arrangement naturally when asked. Rennie handled it professionally.
- **Why Five9:** Landed key points — AI initiatives, project variety, customer-facing nature.
- **Comp:** Deflected initially, Rennie shared range, candidate said "overall number aligns." Did not anchor to a specific number — fine for this stage.
- **CTI gap:** Was NOT asked about it. Good sign — they may weight integration skills over domain-specific CTI.
- **Apple R88 lesson applied:** Did NOT volunteer contractor/non-compete as "why leave." Led with forward attraction.

---

## Intel Gathered

| Item | Detail |
|---|---|
| Hiring Manager | Senior Manager of AI and Custom Services |
| Team size | 5 ICs, senior to principal level |
| Five9 headcount | ~2,900 global |
| Interview process | Recruiter → Manager video → Engineer lineup → PS leader → Legal → Reference + Offer |
| Comp | Base $114.6K–$274.4K + yearly bonus + RSUs (3-yr vest, Nasdaq). No sign-on. |
| H-1B | Five9 does transfers. Green card after 1 year good standing. |
| Remote | Fully remote from TX. Quarterly travel to San Ramon. |
| Referral | Vijay Chinthala — Rennie confirmed "we really like our referrals" |
| Next step | Rennie emails update by tomorrow |

---

## Priority Fixes (ordered by impact)

1. **Fix prompt caching** — 3x TTFT improvement + 85% cost reduction
2. **Fix endpointing** — `endpointing=1000` + minimum 15 words before triggering coaching
3. **Don't clear overlay on new question** — append, don't replace
4. **Add elapsed time to prompt** — coach knows when to wrap up
5. **Fix card relay logging** — track push success/failure
6. **Keyboard shortcuts for quality feedback** — 1-5 keys during interview
7. **Add visa/immigration fields to submission portal**
8. **Add comp standardization templates** — pre-written comp deflection answers

---

## Operator Notes

> "For me as operator, the answers have to stay so that I can refer."
> "Answers are rapidly coming and going — rush for candidate."
> "We should keep all answers scrollable."
> "In between it went blank — answer generated on operator end but card wasn't pushed."
> "We should track total time so the AI model is aware of time progressing."
