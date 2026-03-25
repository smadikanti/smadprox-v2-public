"""
Unified AI Coaching Engine for NoHuman + HumanProx.

Uses Claude Sonnet 4.5 with streaming to generate
contextually-aware interview coaching suggestions.

Both NoHuman (SaaS) and HumanProx (operator-assisted) share the same
prompt system, strategy engine, round-type routing, continuation awareness,
and question-type detection.  The only mode-specific difference: NoHuman's
three-phase filler pipeline injects a "filler bridge continuity" block.
"""

import logging
import anthropic
from typing import AsyncIterator, Optional

from app.config import settings

try:
    from groq import AsyncGroq
    GROQ_AVAILABLE = True
except ImportError:
    GROQ_AVAILABLE = False

logger = logging.getLogger("nohuman.coach")


# ---------------------------------------------------------------------------
# Culture-note filter — extract only the relevant company section
# ---------------------------------------------------------------------------

def filter_culture_notes(company: str, full_notes: str) -> str:
    """Return only the ## section matching *company* from a multi-company
    culture-notes document.  If the company isn't found (or inputs are
    empty), return an empty string so the prompt stays lean."""
    if not company or not full_notes:
        return ""
    sections = full_notes.split("\n## ")
    for section in sections:
        if company.lower() in section[:200].lower():
            return "## " + section
    return ""


# ---------------------------------------------------------------------------
# Unified Coaching System Prompt (shared by NoHuman + HumanProx)
# ---------------------------------------------------------------------------

COACHING_SYSTEM_PROMPT = """You are a real-time interview coach and principal software engineer. Your text is displayed on a teleprompter overlay that the candidate reads during a live interview. Generate EXACTLY what the candidate should say out loud — in FIRST PERSON POV.

═══════════════════════════════════════
VOICE & STYLE
═══════════════════════════════════════
- ALWAYS first person ("I" more than "we"). This is the candidate speaking.
- Use normal conversational vocabulary — NOT complex jargon or formal prose.
- Sound confident but genuine, not scripted or robotic.
- Vary your phrasing each time — NEVER repeat the same wording or opening twice.
- Do NOT start with "Sure," "Great question," "Absolutely," every time — vary openings naturally.
- Do NOT include meta-commentary, stage directions, bullet points, headings, or anything that isn't words to speak.
- SPEED IS CRITICAL — be concise but thorough enough to give a complete answer.
- NO bullet points. NO numbered lists. Write in flowing spoken paragraphs.
- When the interviewer interrupts or redirects mid-answer, pivot gracefully. Use natural transitions like "Right, and actually that ties into..." or "That's a great point — to build on that..." — never sound startled or thrown off. A senior engineer handles redirects with poise.
- The candidate should ALWAYS have something to say. Never generate silence, hesitation markers like "um" or "uh", or empty responses. If unsure, default to a thoughtful bridge: acknowledge what was said, connect it to your experience, and keep the momentum going.

═══════════════════════════════════════
QUESTION TYPE DETECTION & FORMAT
═══════════════════════════════════════
Automatically detect the type of question and adapt your format:

▸ BEHAVIORAL QUESTIONS ("tell me about a time," "describe a situation," "give me an example of," "how did you handle," "can you walk me through"):
  - Use STAR format: 4 flowing paragraphs (Situation, Task, Action, Result).
  - Do NOT write headings like "Situation:" or "S:" — just 4 natural paragraphs that flow as speech.
  - Say more "I" than "we" — emphasize personal contributions.
  - Use specific project names, company names, real metrics from the candidate's resume.
  - If company values/culture keywords are provided, weave them in naturally (don't force them).
  - End with a brief reflection that ties back to the role or company values.

▸ TECHNICAL QUESTIONS (keywords, concepts, "what is," "explain," "how does," "difference between"):
  - Be very concise about what it is, where it's used, applications, pros-cons, differences, alternatives.
  - Then add a short first-person paragraph in simple speaking style: a use-case of how the candidate used this tool/concept, why it was chosen over alternatives. Reference real projects from the resume.

▸ INTRODUCTION / "TELL ME ABOUT YOURSELF":
  - Give a medium-length intro (~60 seconds speaking time).
  - Structure: current role + recent impact → previous role + key achievement → why excited about this role.
  - End with: "This is my generic intro — happy to answer any specific questions you may have."

▸ LONG OR MULTI-PART QUESTIONS:
  - Begin by briefly rephrasing/confirming the question in one sentence (e.g., "So you're asking about how I handled X in the context of Y —"), then provide the answer.

▸ INTERVIEWER ASKS "DO YOU HAVE ANY QUESTIONS FOR ME/US":
  - Generate 1 strong question the candidate should ask.
  - The question should reference something the interviewer mentioned earlier in the conversation (show active listening).
  - Format: brief acknowledgment of what the interviewer said + the actual question.
  - Questions should show senior-level thinking: team dynamics, engineering culture, impact visibility, technical decision-making, how success is measured.

▸ FOLLOW-UP / CLARIFICATION from interviewer on a previous answer:
  - Expand on the specific point they asked about.
  - Add new details, metrics, or context not mentioned in the original answer.
  - Keep it focused — don't re-tell the whole story.

═══════════════════════════════════════
CANDIDATE BACKGROUND
═══════════════════════════════════════
{context_documents}

═══════════════════════════════════════
COMPANY & CULTURE CONTEXT
═══════════════════════════════════════
{custom_prompt}

When company values or culture keywords are provided above, weave them naturally into behavioral answers. Use the exact value names when they fit organically (e.g., "for me, this was about choosing fearlessly to push back" or "I focused on forcing simplicity"). Do NOT force every value into every answer — only use what's relevant.

═══════════════════════════════════════
CONTINUATION AWARENESS
═══════════════════════════════════════
{continuation_block}"""


# ---------------------------------------------------------------------------
# Round-Type Specific Prompts (Strategy Engine)
# ---------------------------------------------------------------------------

# Backward-compatible aliases
DUAL_SYSTEM_PROMPT = COACHING_SYSTEM_PROMPT
SYSTEM_PROMPT = COACHING_SYSTEM_PROMPT


_SHARED_VOICE_RULES = """═══════════════════════════════════════
VOICE & STYLE
═══════════════════════════════════════
- ALWAYS first person ("I" more than "we"). This is the candidate speaking.
- Use normal conversational vocabulary — NOT complex jargon or formal prose.
- Sound confident but genuine, not scripted or robotic.
- Vary your phrasing each time — NEVER repeat the same wording or opening twice.
- Do NOT start with "Sure," "Great question," "Absolutely," every time — vary openings naturally.
- Do NOT include meta-commentary, stage directions, or anything that isn't words to speak.
- SPEED IS CRITICAL — be concise but thorough enough to give a complete answer.
- NO bullet points. NO numbered lists. Write in flowing spoken paragraphs.
- When the interviewer interrupts or redirects, pivot gracefully with natural transitions. A senior engineer handles redirects with poise, never sounds startled.
- The candidate should ALWAYS have something to say. Never generate empty or hesitant responses. If unsure, bridge with experience and keep momentum going."""


_FILLER_BRIDGE_BLOCK = """

═══════════════════════════════════════
FILLER CONTINUITY
═══════════════════════════════════════
The speaker has ALREADY said these words aloud to buy time:
- Filler: "{phase1_filler}"
- Bridge: "{phase2_bridge}"

Your suggestion must CONTINUE NATURALLY from the bridge sentence.
Do NOT repeat the filler or bridge. Start your response so it flows
seamlessly after: "{phase2_bridge}" """


SYSTEM_DESIGN_PROMPT = """You are a real-time interview coach for a SYSTEM DESIGN round. Your text is displayed on a teleprompter overlay that the candidate reads during a live interview. Generate EXACTLY what the candidate should say out loud — in FIRST PERSON POV.

{shared_voice}

{spoken_rules}

═══════════════════════════════════════
SYSTEM DESIGN STRUCTURE
═══════════════════════════════════════
System design interviews follow phases. Generate TWO sections per response:

[WHITEBOARD]
Concise written notes for the whiteboard/drawing tool — components, arrows, labels.
Use plain text notation: "Client -> API Gateway -> Service A, Service B"

[SAY]
What the candidate should actually say out loud, in flowing spoken paragraphs.

PHASES (follow in order, but adapt if interviewer redirects):
1. CLARIFYING QUESTIONS — Ask 3-5 smart clarifying questions before designing anything.
2. REQUIREMENTS — Summarize functional + non-functional requirements.
3. BACK-OF-ENVELOPE — Quick capacity estimation. Round all numbers for speech (say "about ten thousand requests per second" not "9,847 RPS").
4. HIGH-LEVEL ARCHITECTURE — Major components and data flow.
5. API DESIGN — Key endpoints, request/response shapes.
6. DATA MODEL — Core entities, relationships, storage choices.
7. DEEP DIVES — Interviewer picks areas to go deeper. Focus on trade-offs, not just solutions.
8. BOTTLENECKS & SCALING — Where it breaks, how to fix it.

RULES:
- If the interviewer redirects ("let's dig into X"), pivot immediately. Don't finish the current phase.
- Reference the candidate's real experience when explaining trade-offs ("at my previous company we chose X because...").
- Round all numbers for spoken delivery: "about fifty milliseconds" not "47.3ms".
- Say "percent" not "%", "around two hundred" not "~200".

═══════════════════════════════════════
STRATEGY BRIEF
═══════════════════════════════════════
{strategy_brief}

═══════════════════════════════════════
DESIGN STATE
═══════════════════════════════════════
{design_state}

═══════════════════════════════════════
CONTINUATION AWARENESS
═══════════════════════════════════════
{continuation_block}"""


BEHAVIORAL_PROMPT = """You are a real-time interview coach for a BEHAVIORAL round. Your text is displayed on a teleprompter overlay that the candidate reads during a live interview. Generate EXACTLY what the candidate should say out loud — in FIRST PERSON POV.

{shared_voice}

{spoken_rules}

═══════════════════════════════════════
BEHAVIORAL ANSWER FORMAT
═══════════════════════════════════════
Use STAR format as 4 natural flowing paragraphs. Do NOT write headings like "Situation:" — just speak naturally.

Paragraph 1 (Situation): Set the scene — company, team, project, what was happening.
Paragraph 2 (Task): What specifically was your responsibility or challenge.
Paragraph 3 (Action): What YOU did — specific steps, decisions, trade-offs. Say "I" more than "we".
Paragraph 4 (Result): Quantified impact + brief reflection that ties to this role or company values.

RULES:
- Use specific project names, company names, real metrics from the strategy brief.
- Calibrate story scope to the candidate's seniority level.
- Weave in company values when they fit organically. Do NOT force them.
- End with a brief reflection tying back to the role.
- If this is a follow-up, expand on the specific point — don't re-tell the whole story.

═══════════════════════════════════════
STORIES ALREADY TOLD (avoid repetition)
═══════════════════════════════════════
{stories_told}

═══════════════════════════════════════
STRATEGY BRIEF
═══════════════════════════════════════
{strategy_brief}

═══════════════════════════════════════
CONTINUATION AWARENESS
═══════════════════════════════════════
{continuation_block}"""


CODING_PROMPT = """You are a real-time interview coach for a TECHNICAL CODING round. Your text is displayed on a teleprompter overlay that the candidate reads during a live interview. Generate EXACTLY what the candidate should say out loud — in FIRST PERSON POV.

{shared_voice}

{spoken_rules}

═══════════════════════════════════════
CODING INTERVIEW COACHING
═══════════════════════════════════════
Guide the candidate through think-out-loud problem solving. Track where they are in the process:

PHASE 1 — UNDERSTAND: Restate the problem in your own words. Ask 1-2 clarifying questions.
PHASE 2 — APPROACH: Start with brute force, then optimize. Explain the trade-offs out loud.
PHASE 3 — CODE: Talk through the implementation step by step. Explain each decision.
PHASE 4 — TEST: Walk through examples, edge cases, complexity analysis in plain English.

RULES:
- Say "time complexity is about n squared" not "O(n²)".
- Explain data structure choices in plain English: "I'd use a hash map because we need constant-time lookups."
- If stuck, suggest: "Let me think about this differently..." and offer a hint.
- Reference the candidate's experience: "This reminds me of a problem I solved at [company]..."

═══════════════════════════════════════
CODING STATE
═══════════════════════════════════════
{coding_state}

═══════════════════════════════════════
STRATEGY BRIEF
═══════════════════════════════════════
{strategy_brief}

═══════════════════════════════════════
CONTINUATION AWARENESS
═══════════════════════════════════════
{continuation_block}"""


RECRUITER_SCREEN_PROMPT = """You are a real-time interview coach for a RECRUITER SCREEN. Your text is displayed on a teleprompter overlay that the candidate reads during a live interview. Generate EXACTLY what the candidate should say out loud — in FIRST PERSON POV.

{shared_voice}

{spoken_rules}

═══════════════════════════════════════
RECRUITER SCREEN FORMAT
═══════════════════════════════════════
Keep answers SHORT and conversational. This is not a technical deep-dive.

TYPICAL TOPICS:
- "Tell me about yourself" → 30-45 second intro, not the full behavioral version.
- "Why are you leaving?" → Positive framing: growth, new challenges, mission alignment.
- "Why this company?" → Specific reasons tied to the JD and company mission.
- "Compensation expectations?" → If strategy brief includes a range, use it. Otherwise: "I'm flexible and more focused on the right fit — happy to discuss specifics once we've explored mutual interest."
- "Timeline / other processes?" → Be honest but create mild urgency.
- "Visa / location?" → Direct, factual.

RULES:
- 2-4 sentences per answer. Do NOT over-elaborate.
- Sound enthusiastic but not desperate.
- Match the conversational energy — recruiters want to like you.
- Reference 1-2 specific things about the company/role that excite you.

═══════════════════════════════════════
STRATEGY BRIEF
═══════════════════════════════════════
{strategy_brief}

═══════════════════════════════════════
CONTINUATION AWARENESS
═══════════════════════════════════════
{continuation_block}"""


# ---------------------------------------------------------------------------
# Progressive Prompt Disclosure: Per-Question-Type Format Blocks
# ---------------------------------------------------------------------------

_FORMAT_BEHAVIORAL = """RESPONSE FORMAT (Behavioral):
- Use STAR format: 4 flowing paragraphs (Situation, Task, Action, Result).
- Do NOT write headings like "Situation:" — just 4 natural paragraphs that flow as speech.
- Say more "I" than "we" — emphasize personal contributions.
- Use specific project names, company names, real metrics from the candidate's resume.
- If company values/culture keywords are provided, weave them in naturally.
- End with a brief reflection that ties back to the role or company values."""

_FORMAT_TECHNICAL = """RESPONSE FORMAT (Technical):
- Be concise about what it is, where it's used, pros-cons, differences, alternatives.
- Then add a short first-person paragraph: a use-case from the candidate's real experience, why it was chosen over alternatives. Reference real projects."""

_FORMAT_INTRO = """RESPONSE FORMAT (Introduction):
- Give a medium-length intro (~60 seconds speaking time).
- Structure: current role + recent impact → previous role + key achievement → why excited about this role.
- End with: "This is my generic intro — happy to answer any specific questions you may have." """

_FORMAT_QA_INVITE = """RESPONSE FORMAT (Questions for Interviewer):
- Generate 1 strong question the candidate should ask.
- Reference something the interviewer mentioned earlier (show active listening).
- Format: brief acknowledgment of what was said + the actual question.
- Questions should show senior-level thinking: team dynamics, engineering culture, technical decision-making, how success is measured."""

_FORMAT_FOLLOWUP = """RESPONSE FORMAT (Follow-up):
- Expand on the specific point they asked about.
- Add new details, metrics, or context not mentioned in the original answer.
- Keep it focused — don't re-tell the whole story."""

_FORMAT_QUICK = """RESPONSE FORMAT (Quick Answer):
- 1-3 natural sentences MAX. Be direct, confident, grounded in real experience.
- For yes/no: give the answer + a brief evidence hook.
- For pick-one: state your choice + one sentence of reasoning.
- For factual: answer directly with natural tone."""

_FORMAT_LONG = """RESPONSE FORMAT (Multi-Part Question):
- Begin by briefly rephrasing/confirming the question in one sentence, then provide the answer."""

_FORMAT_MAP = {
    "behavioral": _FORMAT_BEHAVIORAL,
    "technical": _FORMAT_TECHNICAL,
    "intro": _FORMAT_INTRO,
    "qa_invite": _FORMAT_QA_INVITE,
    "followup": _FORMAT_FOLLOWUP,
    "quick_answer": _FORMAT_QUICK,
}

_PROGRESSIVE_SYSTEM_PROMPT = """You are a real-time interview coach and principal software engineer. Your text is displayed on a teleprompter overlay that the candidate reads during a live interview. Generate EXACTLY what the candidate should say out loud — in FIRST PERSON POV.

{voice_rules}

═══════════════════════════════════════
{format_block}
═══════════════════════════════════════

═══════════════════════════════════════
CANDIDATE BACKGROUND
═══════════════════════════════════════
{context_documents}

{custom_prompt_block}

═══════════════════════════════════════
CONTINUATION AWARENESS
═══════════════════════════════════════
{continuation_block}"""


# ---------------------------------------------------------------------------
# Structured Conversation State
# ---------------------------------------------------------------------------

GROQ_COMPRESS_PROMPT = """Extract structured state from this interview conversation. Respond ONLY with valid JSON, no markdown fences.

{
  "topics_discussed": ["main topics covered so far"],
  "facts_established": {"key": "value"},
  "pending_questions": ["questions asked but not fully addressed"]
}

Focus on: years of experience, companies, technologies, specific projects, role applied for, strengths shown, concerns raised, compensation discussed."""


async def compress_conversation_state(
    conversation: list[dict],
    existing_state: dict | None = None,
) -> dict:
    """Use Groq to extract structured state from conversation. Called every ~5 turns."""
    if not groq_available():
        return existing_state or {}

    recent = conversation[-12:]
    conv_text = "\n".join(
        f"{'Interviewer' if m.get('role') == 'interviewer' else 'Candidate'}: "
        f"{m.get('content', '')}"
        for m in recent
        if m.get("role") in ("interviewer", "candidate")
    )

    if not conv_text.strip():
        return existing_state or {}

    try:
        client = AsyncGroq(api_key=settings.GROQ_API_KEY)
        response = await client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=[
                {"role": "system", "content": GROQ_COMPRESS_PROMPT},
                {"role": "user", "content": conv_text},
            ],
            max_tokens=400,
            temperature=0,
        )

        import json as _json
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0]
        result = _json.loads(raw)

        old = existing_state or {}
        merged_topics = list(set(old.get("topics_discussed", [])) | set(result.get("topics_discussed", [])))
        merged_facts = {**old.get("facts_established", {}), **result.get("facts_established", {})}

        compressed = {
            "topics_discussed": merged_topics,
            "facts_established": merged_facts,
            "pending_questions": result.get("pending_questions", []),
        }
        logger.info(
            f"[Groq] Conversation compressed: "
            f"{len(merged_topics)} topics, {len(merged_facts)} facts, "
            f"{len(compressed['pending_questions'])} pending"
        )
        return compressed

    except Exception as e:
        logger.warning(f"[Groq] Conversation compression failed: {e}")
        return existing_state or {}


# ---------------------------------------------------------------------------
# Prompt Builders
# ---------------------------------------------------------------------------

def _extract_company_from_docs(context_docs: list[dict]) -> str:
    """Best-effort extraction of company name from context documents."""
    for doc in context_docs:
        dt = doc.get("doc_type", "")
        if dt in ("jd", "job_description"):
            # Company name is often the first word(s) of the JD title
            title = doc.get("title", "")
            if title:
                return title.split(" - ")[0].split(" | ")[0].strip()
        if dt == "script":
            title = doc.get("title", doc.get("name", ""))
            if title:
                return title.split(" - ")[0].split("_")[0].strip()
    return ""


def build_context_block(context_docs: list[dict]) -> str:
    """Format context documents into a readable block.

    Culture-type documents are automatically filtered to the current
    company's section so that irrelevant company notes don't bloat the
    prompt and hurt TTFT.
    """
    if not context_docs:
        return "(No context documents provided)"

    company = _extract_company_from_docs(context_docs)

    sections = []
    for doc in context_docs:
        doc_type = doc.get("doc_type", "notes")
        title = doc.get("title", "Untitled")
        content = doc.get("content", "")

        # Filter culture notes to current company only
        if doc_type in ("culture", "culture_values", "culture_notes") and company:
            filtered = filter_culture_notes(company, content)
            if filtered:
                content = filtered
            # If no match found, keep original (might be single-company doc)

        sections.append(f"[{doc_type.upper()}] {title}:\n{content}")

    return "\n\n".join(sections)


def build_conversation_block(
    conversation: list[dict],
    max_recent: int = 30,
    convo_state: dict | None = None,
) -> str:
    """
    Format conversation history for the prompt.
    When convo_state is available, uses structured summary instead of lossy truncation.
    """
    if not conversation:
        return "(Conversation just started - no exchanges yet)"

    lines = []

    if convo_state and (convo_state.get("topics_discussed") or convo_state.get("facts_established")):
        topics = convo_state.get("topics_discussed", [])
        facts = convo_state.get("facts_established", {})
        pending = convo_state.get("pending_questions", [])

        if topics:
            lines.append(f"[TOPICS COVERED: {', '.join(topics)}]")
        if facts:
            facts_str = "; ".join(f"{k}: {v}" for k, v in facts.items())
            lines.append(f"[ESTABLISHED FACTS: {facts_str}]")
        if pending:
            lines.append(f"[UNANSWERED QUESTIONS: {'; '.join(pending)}]")
        if topics or facts or pending:
            lines.append("")
    elif len(conversation) > max_recent:
        early = conversation[:-max_recent]
        early_text = " ".join(m.get("content", "") for m in early)
        word_count = len(early_text.split())
        lines.append(f"[Earlier in the conversation ({word_count} words): "
                      f"Topics discussed included general introductions "
                      f"and background sharing.]\n")

    recent = conversation[-max_recent:]
    for msg in recent:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if role == "interviewer":
            lines.append(f"THEM (interviewer): {content}")
        elif role == "candidate":
            lines.append(f"YOU (candidate): {content}")
        elif role == "transcript":
            lines.append(f"CONVERSATION: {content}")
        elif role == "suggestion":
            lines.append(f"[SUGGESTED TO CANDIDATE: {content}]")

    return "\n".join(lines)


def _estimate_break_point(suggestion: str, spoken: str) -> int:
    """
    Estimate the word index in the suggestion where the candidate stopped reading.

    Uses a sliding-window match of the last few spoken words against the
    suggestion text. Falls back to a proportional word-count estimate.
    Returns the word index (0-based) in suggestion_words.
    """
    suggestion_words = suggestion.split()
    spoken_words = spoken.split()

    if not spoken_words or not suggestion_words:
        return 0

    suggestion_lower = suggestion.lower()

    # Try to match the last N spoken words in the suggestion
    for window in range(min(6, len(spoken_words)), 0, -1):
        tail = " ".join(spoken_words[-window:]).lower()
        idx = suggestion_lower.rfind(tail)
        if idx != -1:
            words_before = len(suggestion[:idx].split())
            return min(words_before + window, len(suggestion_words))

    # Fallback: proportional estimate
    return min(len(spoken_words), len(suggestion_words))


BUFFER_WORD_COUNT = 3


def build_continuation_block(
    last_suggestion: str,
    candidate_progress: str,
    last_interviewer_text: str,
) -> str:
    """
    Build the continuation-awareness block.

    Handles four scenarios:
      A) Fresh question — no previous suggestion context
      B) Interrupted — candidate was mid-answer, smart buffer continuation
      C) Q&A mode — interviewer answered candidate's question
      D) Rapid-fire — interviewer moved on before candidate could speak
    """
    if not last_suggestion:
        return "SITUATION: This is a fresh question. Generate a complete answer."

    block_lines = [
        "CONTINUATION AWARENESS — READ CAREFULLY:",
        f'FULL PREVIOUS SUGGESTION: "{last_suggestion}"',
    ]

    if candidate_progress and candidate_progress.strip():
        block_lines.append(
            f'WHAT THE CANDIDATE ACTUALLY SAID: "{candidate_progress.strip()}"'
        )
        block_lines.append(
            f'THEN THE INTERVIEWER SAID: "{last_interviewer_text}"'
        )

        # Detect Q&A mode
        candidate_text = candidate_progress.strip().lower()
        is_candidate_question = (
            "?" in candidate_progress
            or "i'm curious" in candidate_text
            or "i would like to understand" in candidate_text
            or "one question" in candidate_text
            or "my question" in candidate_text
        )

        if is_candidate_question:
            block_lines.extend([
                "",
                "Q&A MODE: The candidate just asked the interviewer a question and the interviewer answered.",
                "Your job: generate the candidate's response, which should:",
                "1. Warmly acknowledge what the interviewer shared — reference a SPECIFIC detail they mentioned to prove active listening (1-2 sentences).",
                "2. Blend that naturally into the NEXT question. Use conversational bridges like:",
                '   - "That resonates with me because... which actually makes me wonder..."',
                '   - "I really appreciate that perspective — it connects to something I\'ve been thinking about..."',
                '   - "That\'s exactly the kind of [value/culture keyword] I look for in a team. On a related note..."',
                "3. The next question should demonstrate senior-level thinking: team dynamics, engineering culture, impact visibility, technical decision-making, how success is measured.",
                "4. Weave in company values or culture keywords when natural — show you're already thinking like an insider.",
                "5. The whole response should feel like a genuine conversation between peers, not an interrogation.",
            ])
        else:
            # ── Smart buffer continuation ──
            suggestion_words = last_suggestion.split()
            break_idx = _estimate_break_point(last_suggestion, candidate_progress.strip())

            buffer_end = min(break_idx + BUFFER_WORD_COUNT, len(suggestion_words))
            buffer_words = suggestion_words[break_idx:buffer_end]
            remaining_words = suggestion_words[buffer_end:]

            buffer_text = " ".join(buffer_words) if buffer_words else ""
            remaining_text = " ".join(remaining_words) if remaining_words else ""

            block_lines.extend([
                "",
                "INTERRUPTION RECOVERY — The candidate was mid-answer when the interviewer spoke.",
            ])

            if buffer_text:
                block_lines.extend([
                    f'BUFFER (candidate is about to finish saying these words): "{buffer_text}"',
                    f'UNSAID REMAINDER from original suggestion: "{remaining_text}"' if remaining_text else "UNSAID REMAINDER: (candidate was near the end of the answer)",
                    "",
                    "CRITICAL — Your response MUST start by flowing directly from the buffer words above.",
                    "The candidate will finish saying the buffer, then seamlessly read your new text.",
                    "Do NOT repeat the buffer words. Pick up exactly where they end.",
                ])
            else:
                block_lines.extend([
                    "(candidate was at or near the end of the suggestion)",
                    "",
                ])

            block_lines.extend([
                "",
                "RULES FOR NATURAL BLENDING:",
                "1. Your FIRST words must grammatically and conversationally connect to the buffer. The teleprompter will show: [...buffer words...][YOUR TEXT]. It must read as one fluid sentence.",
                "2. Address the interviewer's interruption naturally — weave it in rather than hard-pivoting. Examples:",
                '   - "...and actually, to your point about X, that\'s exactly what happened —"',
                '   - "...which ties directly into what you\'re asking. So specifically..."',
                '   - "...and I think that connects to your question. The way we handled it was..."',
                "   Vary transitions every time.",
                "3. Incorporate the KEY POINTS from the unsaid remainder — but rephrase them in new words. Do not copy verbatim.",
                "4. Maintain seniority tone and authority throughout. A senior/staff engineer pivots with confidence.",
                "5. Weave in company values or culture keywords where natural.",
                "6. The complete text the candidate reads (buffer + your response) must sound like one uninterrupted, natural spoken answer.",
            ])
    else:
        block_lines.append(
            "WHAT THE CANDIDATE ACTUALLY SAID: (nothing — interviewer spoke again before candidate could respond)"
        )
        block_lines.append(
            f'THE INTERVIEWER THEN SAID: "{last_interviewer_text}"'
        )
        block_lines.extend([
            "",
            "RAPID-FIRE RECOVERY — The interviewer moved on before the candidate could respond to the previous question.",
            "This happens in real interviews. The candidate should NOT look flustered.",
            "",
            "Generate a fresh, confident answer that:",
            "1. Directly addresses what the interviewer JUST said (this is the priority).",
            "2. If any key points from the previous (undelivered) suggestion are still relevant, fold them in naturally — but rephrase completely.",
            "3. Open with a natural transition that makes it feel like the candidate is right on track:",
            '   - "So regarding [topic]..."',
            '   - "That\'s actually something I have direct experience with —"',
            '   - "Great question — I\'ll speak to that from my experience at [company]..."',
            "4. Maintain confident, senior-level tone. No apologies, no 'sorry I didn't get to answer the last one.'",
            "5. Weave in culture values if relevant to the new question.",
        ])

    return "\n".join(block_lines)


def build_coaching_prompt(
    context_docs: list[dict],
    conversation: list[dict],
    custom_prompt: str = "",
    last_suggestion: str = "",
    candidate_progress: str = "",
    last_interviewer_text: str = "",
    strategy_ctx: dict | None = None,
    filler_bridge: tuple[str, str] | None = None,
    question_type: str = "",
    convo_state: dict | None = None,
) -> tuple[str, list[dict]]:
    """
    Unified prompt builder for both NoHuman and HumanProx.

    Progressive disclosure: when question_type is known, only the relevant
    format instructions are injected (not all types). Structured conversation
    state replaces lossy truncation when convo_state is available.

    Returns (system_prompt, messages).
    """
    conversation_block = build_conversation_block(conversation, convo_state=convo_state)
    continuation_block = build_continuation_block(
        last_suggestion, candidate_progress, last_interviewer_text
    )

    q_type = question_type or _detect_question_type(last_interviewer_text or "")

    # --- Build system prompt (strategy-aware routing) ---
    if strategy_ctx and strategy_ctx.get("strategy_brief"):
        round_type = strategy_ctx.get("round_type", "general")
        brief = strategy_ctx["strategy_brief"]
        spoken = strategy_ctx.get("spoken_rules", "")

        if round_type == "system_design":
            ds = _format_design_state(strategy_ctx.get("design_state", {}))
            system = SYSTEM_DESIGN_PROMPT.format(
                shared_voice=_SHARED_VOICE_RULES,
                spoken_rules=spoken,
                strategy_brief=brief,
                design_state=ds,
                continuation_block=continuation_block,
            )
        elif round_type == "behavioral":
            told = strategy_ctx.get("stories_told", [])
            told_str = "\n".join(f"- {s}" for s in told) if told else "(none yet)"
            system = BEHAVIORAL_PROMPT.format(
                shared_voice=_SHARED_VOICE_RULES,
                spoken_rules=spoken,
                stories_told=told_str,
                strategy_brief=brief,
                continuation_block=continuation_block,
            )
        elif round_type == "technical_coding":
            cs = _format_coding_state(strategy_ctx.get("coding_state", {}))
            system = CODING_PROMPT.format(
                shared_voice=_SHARED_VOICE_RULES,
                spoken_rules=spoken,
                coding_state=cs,
                strategy_brief=brief,
                continuation_block=continuation_block,
            )
        elif round_type == "recruiter_screen":
            system = RECRUITER_SCREEN_PROMPT.format(
                shared_voice=_SHARED_VOICE_RULES,
                spoken_rules=spoken,
                strategy_brief=brief,
                continuation_block=continuation_block,
            )
        else:
            context_block = f"[PRE-COMPILED STRATEGY BRIEF]\n{brief}"
            custom_block = custom_prompt if custom_prompt else ""
            system = COACHING_SYSTEM_PROMPT.format(
                context_documents=context_block,
                custom_prompt=custom_block,
                continuation_block=continuation_block,
            )
    elif q_type and q_type != "general" and q_type in _FORMAT_MAP:
        # Progressive disclosure: slim prompt with only the relevant format
        context_block = build_context_block(context_docs)
        custom_block = (
            f"\n═══════════════════════════════════════\n"
            f"COMPANY & CULTURE CONTEXT\n"
            f"═══════════════════════════════════════\n"
            f"{custom_prompt}"
        ) if custom_prompt else ""
        system = _PROGRESSIVE_SYSTEM_PROMPT.format(
            voice_rules=_SHARED_VOICE_RULES,
            format_block=_FORMAT_MAP[q_type],
            context_documents=context_block,
            custom_prompt_block=custom_block,
            continuation_block=continuation_block,
        )
    else:
        context_block = build_context_block(context_docs)
        custom_block = custom_prompt if custom_prompt else ""
        system = COACHING_SYSTEM_PROMPT.format(
            context_documents=context_block,
            custom_prompt=custom_block,
            continuation_block=continuation_block,
        )

    # --- Append filler bridge block if present (NoHuman three-phase path) ---
    if filler_bridge:
        phase1, phase2 = filler_bridge
        system += _FILLER_BRIDGE_BLOCK.format(
            phase1_filler=phase1,
            phase2_bridge=phase2,
        )

    # --- Build user message with question type hint ---
    type_hint = _build_type_hint(q_type)

    if filler_bridge:
        phase1, phase2 = filler_bridge
        user_message = f"""CONVERSATION SO FAR:
{conversation_block}

The other person just finished speaking. The speaker has already said: "{phase1} {phase2}"
{type_hint}
Continue naturally from the bridge. What should they say next?"""
    else:
        user_message = f"""CONVERSATION SO FAR:
{conversation_block}

The interviewer just said: "{last_interviewer_text}"
{type_hint}
Generate what the candidate should say next. Write ONLY the words to speak — first person, no bullet points, no commentary."""

    messages = [{"role": "user", "content": user_message}]

    return system, messages


def _build_type_hint(q_type: str) -> str:
    """Build a concise type hint for the user message based on classified question type."""
    hints = {
        "quick_answer": "\n[QUICK-ANSWER — respond in 1-2 natural sentences MAX. Direct, confident, grounded in real experience.]",
        "behavioral": "\n[BEHAVIORAL — use STAR format, 4 paragraphs, first person, real metrics from resume]",
        "intro": "\n[INTRODUCTION — medium intro: current role, previous role, why this role excites you]",
        "qa_invite": "\n[QUESTIONS FOR INTERVIEWER — 1 strong senior-level question referencing something discussed earlier]",
        "followup": "\n[FOLLOW-UP — expand on the specific point, add new details and metrics, don't re-tell the whole story]",
        "technical": "\n[TECHNICAL — concise explanation + first-person experience from real projects]",
    }
    return hints.get(q_type, "")


def build_prompt(
    context_docs: list[dict],
    conversation: list[dict],
    custom_prompt: str,
    phase1_filler: str,
    phase2_bridge: str,
    last_suggestion: str = "",
    candidate_progress: str = "",
    last_interviewer_text: str = "",
    strategy_ctx: dict | None = None,
) -> tuple[str, list[dict]]:
    """Backward-compatible wrapper — NoHuman filler path."""
    return build_coaching_prompt(
        context_docs=context_docs,
        conversation=conversation,
        custom_prompt=custom_prompt,
        last_suggestion=last_suggestion,
        candidate_progress=candidate_progress,
        last_interviewer_text=last_interviewer_text,
        strategy_ctx=strategy_ctx,
        filler_bridge=(phase1_filler, phase2_bridge),
    )


def _detect_question_type(text: str) -> str:
    """
    Heuristic to detect what kind of question the interviewer asked.
    Returns one of: 'quick_answer', 'behavioral', 'technical', 'intro',
    'qa_invite', 'followup', 'general'.
    """
    t = text.lower().strip()
    word_count = len(t.split())

    # Interviewer inviting candidate to ask questions (check FIRST —
    # these are short and would otherwise hit the quick-answer heuristic)
    qa_triggers = [
        "any questions for me", "any questions for us",
        "do you have any questions", "questions you'd like to ask",
        "open up for questions", "what questions do you have",
        "leave some time for your questions", "open it up to",
    ]
    if any(trigger in t for trigger in qa_triggers):
        return "qa_invite"

    # ── Quick-answer questions (short, factual, yes/no, pick-one) ──
    quick_yn = [
        "are you comfortable", "have you used", "have you worked with",
        "do you have experience", "are you familiar", "do you know",
        "have you ever", "is that correct", "does that make sense",
        "are you open to", "would you be willing", "can you start",
        "are you currently", "do you prefer",
    ]
    quick_factual = [
        "how many years", "what's your notice", "where are you located",
        "where are you based", "what's your expected", "what is your current",
        "when can you start", "what's your visa", "what is your visa",
        "what timezone", "what time zone",
    ]
    quick_pick = [
        "which database", "which language", "what language",
        "which framework", "what framework", "what tool",
        "which tool", "what would you pick", "what would you choose",
        "sql or nosql", "rest or graphql", "monolith or microservice",
    ]
    if any(trigger in t for trigger in quick_yn + quick_factual + quick_pick):
        return "quick_answer"
    # Very short utterances (under 8 words with ?) that aren't caught above,
    # but exclude design/deep-dive prompts that happen to be short
    deep_signals = ["design", "implement", "build", "architect", "walk me", "explain how"]
    if word_count <= 7 and "?" in text and not any(s in t for s in deep_signals):
        return "quick_answer"

    # Behavioral patterns
    behavioral_triggers = [
        "tell me about a time", "describe a situation",
        "give me an example", "can you walk me through",
        "how did you handle", "share an experience",
        "tell me about a project", "tell me about the complex",
        "can you tell me about a time", "have you ever had to",
        "what would you do if", "how would you approach",
        "tell me about when", "can you share",
    ]
    if any(trigger in t for trigger in behavioral_triggers):
        return "behavioral"

    # Intro patterns
    intro_triggers = [
        "tell me about yourself", "introduce yourself",
        "walk me through your background", "tell us about yourself",
        "brief introduction", "start with your background",
    ]
    if any(trigger in t for trigger in intro_triggers):
        return "intro"

    # Follow-up / clarification
    followup_triggers = [
        "can you elaborate", "can you explain more",
        "what do you mean by", "could you go deeper",
        "tell me more about", "sorry i missed",
        "can you expand on", "bit more on",
    ]
    if any(trigger in t for trigger in followup_triggers):
        return "followup"

    return "general"


def build_strategy_context(
    strategy_brief: str = "",
    round_type: str = "general",
    spoken_rules: str = "",
    design_state: dict | None = None,
    stories_told: list[str] | None = None,
    coding_state: dict | None = None,
) -> dict:
    """Package strategy engine state into a dict for prompt builders."""
    return {
        "strategy_brief": strategy_brief,
        "round_type": round_type,
        "spoken_rules": spoken_rules,
        "design_state": design_state or {},
        "stories_told": stories_told or [],
        "coding_state": coding_state or {},
    }


def _format_design_state(design_state: dict) -> str:
    phases = design_state.get("phases_covered", [])
    current = design_state.get("current_phase", "")
    whiteboard = design_state.get("whiteboard_content", "")
    reactions = design_state.get("interviewer_reactions", [])

    lines = []
    if phases:
        lines.append(f"Phases covered: {', '.join(phases)}")
    if current:
        lines.append(f"Current phase: {current}")
    if whiteboard:
        lines.append(f"Whiteboard so far:\n{whiteboard}")
    if reactions:
        lines.append(f"Interviewer asked about: {', '.join(reactions)}")
    lines.append("Continue from where we left off. Do NOT repeat covered phases.")
    return "\n".join(lines) if lines else "Starting fresh — begin with clarifying questions."


def _format_coding_state(coding_state: dict) -> str:
    lines = []
    if coding_state.get("problem_understood"):
        lines.append("Problem has been restated/understood.")
    if coding_state.get("approach_discussed"):
        lines.append("Approach (brute force and/or optimal) has been discussed.")
    if coding_state.get("coding_started"):
        lines.append("Coding/implementation has begun.")
    if coding_state.get("testing_done"):
        lines.append("Testing/edge cases have been covered.")
    if not lines:
        return "Starting fresh — begin by understanding the problem."
    return "\n".join(lines)


def build_dual_prompt(
    context_docs: list[dict],
    conversation: list[dict],
    custom_prompt: str,
    last_suggestion: str = "",
    candidate_progress: str = "",
    last_interviewer_text: str = "",
    strategy_ctx: dict | None = None,
) -> tuple[str, list[dict]]:
    """Backward-compatible wrapper — HumanProx path."""
    return build_coaching_prompt(
        context_docs=context_docs,
        conversation=conversation,
        custom_prompt=custom_prompt,
        last_suggestion=last_suggestion,
        candidate_progress=candidate_progress,
        last_interviewer_text=last_interviewer_text,
        strategy_ctx=strategy_ctx,
    )


# ---------------------------------------------------------------------------
# Unified Streaming Coaching Generator
# ---------------------------------------------------------------------------

async def generate_coaching(
    context_docs: list[dict],
    conversation: list[dict],
    custom_prompt: str = "",
    last_suggestion: str = "",
    candidate_progress: str = "",
    last_interviewer_text: str = "",
    strategy_ctx: dict | None = None,
    filler_bridge: tuple[str, str] | None = None,
    max_tokens: int = 4096,
    question_type: str = "",
    convo_state: dict | None = None,
    stats_out: dict | None = None,
) -> AsyncIterator[str]:
    """
    Unified coaching generator for both NoHuman and HumanProx.
    Yields text chunks as they arrive from Claude.

    question_type: pre-classified question type for progressive prompt disclosure.
    convo_state: structured conversation state (topics, facts, pending Qs).
    """
    try:
        client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

        system, messages = build_coaching_prompt(
            context_docs=context_docs,
            conversation=conversation,
            custom_prompt=custom_prompt,
            last_suggestion=last_suggestion,
            candidate_progress=candidate_progress,
            last_interviewer_text=last_interviewer_text,
            strategy_ctx=strategy_ctx,
            filler_bridge=filler_bridge,
            question_type=question_type,
            convo_state=convo_state,
        )

        # Route simple questions to Haiku for faster TTFT
        HAIKU_ELIGIBLE = {"quick_answer", "follow_up", "followup", "clarification", "yes_no"}
        model = settings.CLAUDE_MODEL
        if question_type in HAIKU_ELIGIBLE:
            model = settings.HAIKU_MODEL

        # Convert system prompt to content blocks with explicit cache_control
        # This enables prompt caching — the system prompt is cached across questions
        system_blocks = [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        async with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system_blocks,
            messages=messages,
        ) as stream:
            async for text in stream.text_stream:
                yield text

            # Log cache stats after stream completes
            try:
                final_msg = await stream.get_final_message()
                usage = final_msg.usage
                cache_read = getattr(usage, 'cache_read_input_tokens', 0) or 0
                cache_create = getattr(usage, 'cache_creation_input_tokens', 0) or 0
                input_tokens = getattr(usage, 'input_tokens', 0) or 0
                output_tokens = getattr(usage, 'output_tokens', 0) or 0
                logger.info(
                    f"[Cache] model={model} input={input_tokens} output={output_tokens} "
                    f"cache_read={cache_read} cache_create={cache_create} "
                    f"hit={'YES' if cache_read > 0 else 'no'}"
                )
                # Populate stats_out for metrics tracking
                if stats_out is not None:
                    stats_out['model_used'] = model
                    stats_out['cache_hit'] = cache_read > 0
                    stats_out['cache_read_tokens'] = cache_read
                    stats_out['input_tokens'] = input_tokens
                    stats_out['output_tokens'] = output_tokens
            except Exception as cache_err:
                logger.debug(f"[Cache] Could not read usage stats: {cache_err}")
    except anthropic.APIConnectionError as e:
        logger.error(f"Claude API connection error: {e}")
        yield "[Connection error — retrying next turn]"
    except anthropic.RateLimitError as e:
        logger.warning(f"Claude rate-limited: {e}")
        yield "[Rate-limited — waiting for next turn]"
    except anthropic.APIStatusError as e:
        logger.error(f"Claude API error {e.status_code}: {e.message}")
        yield f"[API error {e.status_code}]"
    except Exception as e:
        logger.exception(f"Unexpected error in generate_coaching: {e}")
        yield "[Error generating suggestion]"


# Backward-compatible wrappers -------------------------------------------------

async def generate_suggestion(
    context_docs: list[dict],
    conversation: list[dict],
    custom_prompt: str,
    phase1_filler: str,
    phase2_bridge: str,
    last_suggestion: str = "",
    candidate_progress: str = "",
    last_interviewer_text: str = "",
    strategy_ctx: dict | None = None,
) -> AsyncIterator[str]:
    """Backward-compatible wrapper — NoHuman filler path."""
    async for chunk in generate_coaching(
        context_docs=context_docs,
        conversation=conversation,
        custom_prompt=custom_prompt,
        last_suggestion=last_suggestion,
        candidate_progress=candidate_progress,
        last_interviewer_text=last_interviewer_text,
        strategy_ctx=strategy_ctx,
        filler_bridge=(phase1_filler, phase2_bridge),
        max_tokens=250,
    ):
        yield chunk


async def generate_dual_coaching(
    context_docs: list[dict],
    conversation: list[dict],
    custom_prompt: str,
    last_suggestion: str = "",
    candidate_progress: str = "",
    last_interviewer_text: str = "",
    strategy_ctx: dict | None = None,
) -> AsyncIterator[str]:
    """Backward-compatible wrapper — HumanProx path."""
    async for chunk in generate_coaching(
        context_docs=context_docs,
        conversation=conversation,
        custom_prompt=custom_prompt,
        last_suggestion=last_suggestion,
        candidate_progress=candidate_progress,
        last_interviewer_text=last_interviewer_text,
        strategy_ctx=strategy_ctx,
    ):
        yield chunk


# ---------------------------------------------------------------------------
# Groq Fast-Flash: Ultra-low-latency initial response (~200ms TTFT)
# ---------------------------------------------------------------------------

GROQ_FLASH_PROMPT = """You are a real-time interview coach. Generate a quick 1-2 sentence preview of what the candidate should start saying while a more detailed answer is being prepared.

Rules:
- Write in FIRST PERSON as the candidate speaking.
- 1-2 sentences max — this is a quick opener, not the full answer.
- Sound natural and confident.
- If it's a behavioral question, start with the situation setup.
- If it's technical, start with a concise definition.
- If it's "tell me about yourself," start with the opening line.
- Do NOT include meta-commentary. Write ONLY words to speak."""


# ---------------------------------------------------------------------------
# Groq Quick-Answer: Complete, natural short answers (~200ms)
# ---------------------------------------------------------------------------

GROQ_QUICK_ANSWER_PROMPT = """You are coaching a candidate in a LIVE interview. The interviewer asked a quick question that needs an immediate, concise answer.

Your job: write the EXACT words the candidate should say out loud — and nothing else.

RULES:
1. FIRST PERSON only. You ARE the candidate.
2. 1-3 sentences MAX. If a single confident sentence works, prefer it.
3. Sound like a real human who is well prepared, NOT a textbook or chatbot.
4. Ground every answer in the candidate's ACTUAL experience and background. Never invent credentials.
5. Match the seniority level — a senior engineer answers with casual authority ("Yeah, I've spent the last three years deep in Kafka"), a mid-level answers with honest confidence ("I've worked with it on two production projects").
6. For yes/no: NEVER just say "yes" or "no". Give the answer + a brief evidence hook.
   Good: "Yeah, absolutely — I built our entire event pipeline on Kafka, handled about two million events per day."
   Bad: "Yes, I have experience with Kafka."
7. For pick-one: State your choice + one sentence of reasoning from real experience.
   Good: "I'd go with PostgreSQL — we used it at my last company for a similar read-heavy workload and it scaled really well with proper indexing."
   Bad: "PostgreSQL is a good choice because it supports ACID transactions."
8. For factual (years, location, notice): Answer directly with a natural, human tone.
   Good: "About eight years now, most of that focused on distributed systems."
   Bad: "I have eight years of experience."
9. NO meta-commentary, NO quotation marks, NO labels, NO markdown. ONLY speakable words.
10. Use natural spoken patterns — contractions, occasional "yeah" or "honestly", slight hedges that sound human.
11. Weave in company culture values when they naturally fit (don't force it).
12. SPOKEN LANGUAGE: write numbers as words (eight, not 8), no symbols, no abbreviations."""


def groq_available() -> bool:
    """Check if Groq fast-path is configured and available."""
    return GROQ_AVAILABLE and bool(settings.GROQ_API_KEY)


async def generate_groq_flash(
    conversation: list[dict],
    last_interviewer_text: str,
    context_summary: str = "",
) -> Optional[str]:
    """
    Generate an ultra-fast 1-2 sentence preview via Groq LPU.
    Returns the flash text, or None if Groq is unavailable/fails.
    """
    if not groq_available():
        return None

    try:
        client = AsyncGroq(api_key=settings.GROQ_API_KEY)

        # Build a minimal context for speed
        recent = conversation[-6:] if conversation else []
        conv_text = "\n".join(
            f"{'Interviewer' if m.get('role') == 'interviewer' else 'Candidate'}: "
            f"{m.get('content', '')}"
            for m in recent
            if m.get("role") in ("interviewer", "candidate")
        )

        user_msg = f"""Recent conversation:
{conv_text}

The interviewer just said: "{last_interviewer_text}"

{f'Candidate background: {context_summary[:500]}' if context_summary else ''}

Generate a quick 1-2 sentence opener the candidate should start saying NOW."""

        response = await client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=[
                {"role": "system", "content": GROQ_FLASH_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=80,
            temperature=0.7,
        )

        flash = response.choices[0].message.content.strip()
        logger.info(f"[Groq] Flash generated ({len(flash.split())} words)")
        return flash

    except Exception as e:
        logger.warning(f"[Groq] Flash generation failed (non-blocking): {e}")
        return None


def detect_quick_answer(text: str) -> bool:
    """Public helper: returns True if the interviewer's utterance is a quick-answer question."""
    return _detect_question_type(text) == "quick_answer"


# ---------------------------------------------------------------------------
# Groq Question Classifier (~50ms, replaces keyword heuristics)
# ---------------------------------------------------------------------------

GROQ_CLASSIFIER_PROMPT = """Classify this interview question into exactly ONE category. Respond with ONLY the category name, nothing else.

Categories:
- quick_answer: yes/no, factual, pick-one, short-answer (location, years, tools, preferences)
- behavioral: "tell me about a time", situational examples, conflict, leadership stories
- technical: concept explanations, "what is", "how does", architecture questions
- intro: "tell me about yourself", background overview
- qa_invite: interviewer asking "do you have any questions for me/us"
- followup: expanding on a previous answer, "can you elaborate", "tell me more"
- general: everything else"""


async def classify_question_groq(text: str) -> Optional[str]:
    """Ultra-fast question classification via Groq LPU (~50ms)."""
    if not groq_available() or not text.strip():
        return None
    try:
        client = AsyncGroq(api_key=settings.GROQ_API_KEY)
        response = await client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=[
                {"role": "system", "content": GROQ_CLASSIFIER_PROMPT},
                {"role": "user", "content": f'Question: "{text}"'},
            ],
            max_tokens=10,
            temperature=0,
        )
        raw = response.choices[0].message.content.strip().lower()
        result = raw.replace(" ", "_").replace("-", "_")
        valid = {"quick_answer", "behavioral", "technical", "intro", "qa_invite", "followup", "general"}
        if result in valid:
            logger.info(f"[Groq] Classified as '{result}': {text[:60]}")
            return result
        logger.warning(f"[Groq] Invalid classification '{result}', falling back")
        return None
    except Exception as e:
        logger.warning(f"[Groq] Classification failed: {e}")
        return None


async def classify_question(text: str) -> str:
    """Classify question type. Tries Groq first (~50ms), falls back to keyword heuristic."""
    groq_result = await classify_question_groq(text)
    if groq_result:
        return groq_result
    return _detect_question_type(text)


async def generate_groq_quick_answer(
    conversation: list[dict],
    last_interviewer_text: str,
    context_docs: list[dict] | None = None,
    strategy_brief: str = "",
    seniority_level: str = "mid",
    spoken_rules: str = "",
    culture_values: str = "",
) -> Optional[str]:
    """
    Generate a complete, natural short answer via Groq LPU for quick questions.

    Unlike generate_groq_flash (preview while Claude loads), this IS the final answer.
    Returns the answer text, or None if Groq is unavailable/fails (caller should
    fall back to Claude).
    """
    if not groq_available():
        return None

    try:
        client = AsyncGroq(api_key=settings.GROQ_API_KEY)

        recent = conversation[-6:] if conversation else []
        conv_text = "\n".join(
            f"{'Interviewer' if m.get('role') == 'interviewer' else 'Candidate'}: "
            f"{m.get('content', '')}"
            for m in recent
            if m.get("role") in ("interviewer", "candidate")
        )

        # Extract resume summary from context docs
        resume_summary = ""
        for doc in (context_docs or []):
            if doc.get("doc_type") == "resume":
                resume_summary = doc.get("content", "")[:800]
                break

        # Use strategy brief if available (richer than raw resume)
        background = strategy_brief[:600] if strategy_brief else resume_summary

        seniority_hint = {
            "junior": "Sound like a motivated, honest early-career engineer.",
            "mid": "Sound like a confident mid-level engineer with solid hands-on experience.",
            "senior": "Sound like a seasoned senior engineer — casual authority, depth when needed.",
            "staff": "Sound like a staff/principal engineer — strategic thinking, broad ownership, effortless expertise.",
        }.get(seniority_level, "Sound confident and experienced.")

        culture_block = ""
        if culture_values:
            culture_block = f"\nCompany values to subtly reflect: {culture_values[:200]}"

        spoken_block = ""
        if spoken_rules:
            spoken_block = f"\nSpoken rules: {spoken_rules[:200]}"

        user_msg = f"""Recent conversation:
{conv_text}

Interviewer just asked: "{last_interviewer_text}"

Candidate background:
{background}

Seniority: {seniority_level} — {seniority_hint}{culture_block}{spoken_block}

Generate the candidate's answer. This is a quick question — answer in 1-3 natural sentences."""

        response = await client.chat.completions.create(
            model=settings.GROQ_MODEL,
            messages=[
                {"role": "system", "content": GROQ_QUICK_ANSWER_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_tokens=300,
            temperature=0.6,
        )

        answer = response.choices[0].message.content.strip()
        logger.info(f"[Groq] Quick answer generated ({len(answer.split())} words)")
        return answer

    except Exception as e:
        logger.warning(f"[Groq] Quick answer failed (falling back to Claude): {e}")
        return None
