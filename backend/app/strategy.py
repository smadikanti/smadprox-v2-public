"""
Pre-Session Strategy Compiler for NoHuman.

Analyzes all context docs (resume, JD, interviewer LinkedIn, culture doc,
recruiter notes) in a single Claude call and produces a condensed strategy
brief (~1500 tokens) that replaces raw docs in per-turn prompts.

Better signal, fewer tokens, faster generation.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import anthropic

from app.config import settings

logger = logging.getLogger("nohuman.strategy")


# ---------------------------------------------------------------------------
# Data Types
# ---------------------------------------------------------------------------

@dataclass
class StrategyGap:
    """A gap identified during strategy compilation that needs operator input."""
    id: str
    question: str
    options: list[str]  # empty list = freetext input


@dataclass
class StrategyBrief:
    seniority_level: str  # "junior" | "mid" | "senior" | "staff"
    round_type: str  # "system_design" | "behavioral" | "technical_coding" | "recruiter_screen" | "general"
    brief_text: str  # The compiled strategy (~1500 tokens)
    spoken_rules: str  # Calibrated voice rules for this seniority level
    gaps: list[StrategyGap] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Round-Type Detection (from doc metadata or explicit selector)
# ---------------------------------------------------------------------------

def detect_round_type(context_docs: list[dict], explicit_round_type: str = "") -> str:
    if explicit_round_type and explicit_round_type != "general":
        return explicit_round_type

    for doc in context_docs:
        dt = doc.get("doc_type", "").lower()
        content = doc.get("content", "").lower()
        if dt == "round_type":
            if "system design" in content or "system_design" in content:
                return "system_design"
            if "behavioral" in content:
                return "behavioral"
            if "coding" in content or "technical" in content:
                return "technical_coding"
            if "recruiter" in content or "screen" in content:
                return "recruiter_screen"

    return "general"


# ---------------------------------------------------------------------------
# Seniority-Calibrated Spoken Rules
# ---------------------------------------------------------------------------

SPOKEN_RULES = {
    "junior": (
        "SPOKEN LANGUAGE RULES (Junior/Mid Level):\n"
        "- Keep answers focused and concise — don't overreach on scope.\n"
        "- Use 'I' more than 'we' — emphasize your direct contributions.\n"
        "- It's OK to say 'I haven't worked with X directly, but here's how I'd approach it.'\n"
        "- Numbers should be rounded: say 'about fifty' not '47.3'.\n"
        "- No symbols in speech: say 'percent' not '%', 'around two hundred milliseconds' not '~200ms'.\n"
        "- No abbreviations unless universally spoken (OK: API, AWS. Avoid: LGTM, PTAL)."
    ),
    "mid": (
        "SPOKEN LANGUAGE RULES (Mid Level):\n"
        "- Show ownership of projects and decisions you drove.\n"
        "- Use 'I' more than 'we' — emphasize your direct contributions.\n"
        "- Scope stories to your team or feature area — not company-wide impact.\n"
        "- Numbers should be rounded: say 'about fifty' not '47.3'.\n"
        "- No symbols in speech: say 'percent' not '%', 'around two hundred milliseconds' not '~200ms'.\n"
        "- No abbreviations unless universally spoken (OK: API, AWS. Avoid: LGTM, PTAL)."
    ),
    "senior": (
        "SPOKEN LANGUAGE RULES (Senior Level):\n"
        "- Show cross-team impact and influence without authority.\n"
        "- Use 'I' for decisions you made, 'we' when crediting team execution.\n"
        "- Scope stories to cross-team or org-level impact.\n"
        "- Mention mentoring, design reviews, or technical direction you set.\n"
        "- Numbers should be rounded: say 'about fifty' not '47.3'.\n"
        "- No symbols in speech: say 'percent' not '%', 'around two hundred milliseconds' not '~200ms'.\n"
        "- No abbreviations unless universally spoken (OK: API, AWS. Avoid: LGTM, PTAL)."
    ),
    "staff": (
        "SPOKEN LANGUAGE RULES (Staff+ Level):\n"
        "- Demonstrate company-wide or multi-org technical strategy.\n"
        "- Frame problems as business impact, not just technical decisions.\n"
        "- Show how you identified and drove work that wasn't asked for.\n"
        "- Use 'I' for strategy and vision, 'we' for execution.\n"
        "- Reference architectural decisions, RFCs, or migrations you led.\n"
        "- Numbers should be rounded: say 'about fifty' not '47.3'.\n"
        "- No symbols in speech: say 'percent' not '%', 'around two hundred milliseconds' not '~200ms'.\n"
        "- No abbreviations unless universally spoken (OK: API, AWS. Avoid: LGTM, PTAL)."
    ),
}


# ---------------------------------------------------------------------------
# Strategy Compilation Prompt
# ---------------------------------------------------------------------------

STRATEGY_COMPILATION_PROMPT = """You are an interview strategy compiler. Analyze the following context and produce a condensed coaching strategy brief.

Your output will be injected into a real-time interview coaching system. It must be dense, actionable, and under 1500 tokens. No fluff.

{resume_block}
{jd_block}
{interviewer_block}
{culture_block}
{recruiter_block}
ROUND TYPE: {round_type}

Produce the following sections — be specific, use names/metrics from the resume, and tailor to this exact role:

1. SENIORITY ASSESSMENT
What level is this candidate (junior/mid/senior/staff)? What scope of stories is appropriate? One paragraph.

2. FIT ANALYSIS
- Top 3 strengths that match the JD (be specific — cite projects, technologies, outcomes)
- Top 2 gaps to handle carefully (missing skills, short tenures, industry mismatch)

3. INTERVIEWER PROFILE
What this interviewer likely evaluates based on their background. What would impress them specifically. If no interviewer info, provide general guidance for the round type.

4. VALUE MAPPING
For each company value (if provided), map ONE real experience from the resume that demonstrates it. Use project names and metrics.

5. INTRO SCRIPT
A tailored introduction for this role (~45 seconds speaking time). Current role + key achievement → why this role excites the candidate. First person. Write it as speakable text.

6. TOP STORIES
3 behavioral stories from the resume, each in one sentence, mapped to likely question themes (conflict, technical challenge, leadership, failure, impact). Include the project name and metric.

7. WATCH-OUTS
Things to be careful about: short tenures, missing skills, potential weaknesses. How to address each if asked.

8. GAPS
List 2-4 specific questions where the operator's answer would meaningfully improve coaching quality, but the answer cannot be determined from the provided documents. Use this EXACT format for each gap:
GAP: [question text] | OPTIONS: [option1, option2, option3]
OR for freetext:
GAP: [question text] | FREETEXT

Examples:
GAP: What seniority level is the candidate targeting? | OPTIONS: Junior, Mid, Senior, Staff+
GAP: Which project from their resume should we emphasize most? | FREETEXT
GAP: Is compensation already discussed with the recruiter? | OPTIONS: Yes - range agreed, No - avoid topic, Unknown
GAP: What's the candidate's biggest concern about this role? | FREETEXT

Only include gaps where the answer would meaningfully change the coaching strategy. If all key info is provided, write: GAP: none"""


def _extract_doc(context_docs: list[dict], doc_type: str) -> str:
    for doc in context_docs:
        if doc.get("doc_type", "").lower() == doc_type:
            return doc.get("content", "")
    return ""


def _build_section(label: str, content: str) -> str:
    if content.strip():
        return f"{label}:\n{content.strip()}"
    return f"{label}:\n(Not provided)"


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

async def compile_strategy(
    context_docs: list[dict],
    explicit_round_type: str = "",
) -> StrategyBrief:
    """
    Run a one-time Claude call to compile all context into a targeted strategy brief.
    Falls back gracefully if the API call fails.
    """
    round_type = detect_round_type(context_docs, explicit_round_type)

    resume = _extract_doc(context_docs, "resume")
    jd = _extract_doc(context_docs, "jd")
    interviewer = _extract_doc(context_docs, "interviewer_profile")
    culture = _extract_doc(context_docs, "company_values") or _extract_doc(context_docs, "culture_values")
    recruiter = _extract_doc(context_docs, "recruiter_prep")

    if not resume and not jd:
        logger.info("[Strategy] No resume or JD provided — skipping compilation")
        return StrategyBrief(
            seniority_level="mid",
            round_type=round_type,
            brief_text="",
            spoken_rules=SPOKEN_RULES["mid"],
        )

    prompt = STRATEGY_COMPILATION_PROMPT.format(
        resume_block=_build_section("RESUME", resume),
        jd_block=_build_section("JOB DESCRIPTION", jd),
        interviewer_block=_build_section("INTERVIEWER PROFILE", interviewer),
        culture_block=_build_section("CULTURE / VALUES", culture),
        recruiter_block=_build_section("RECRUITER PREP NOTES", recruiter),
        round_type=round_type,
    )

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

        response = await client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=2000,
            system="You are a senior interview coach. Produce a concise, actionable strategy brief.",
            messages=[{"role": "user", "content": prompt}],
        )

        brief_text = response.content[0].text

        seniority = _parse_seniority(brief_text)
        spoken_rules = SPOKEN_RULES.get(seniority, SPOKEN_RULES["mid"])
        gaps = _parse_gaps(brief_text)

        logger.info(
            f"[Strategy] Compiled: round={round_type}, seniority={seniority}, "
            f"brief_len={len(brief_text)} chars, gaps={len(gaps)}"
        )

        return StrategyBrief(
            seniority_level=seniority,
            round_type=round_type,
            brief_text=brief_text,
            spoken_rules=spoken_rules,
            gaps=gaps,
        )

    except Exception as e:
        logger.error(f"[Strategy] Compilation failed (non-blocking): {e}")
        return StrategyBrief(
            seniority_level="mid",
            round_type=round_type,
            brief_text="",
            spoken_rules=SPOKEN_RULES["mid"],
        )


def _parse_seniority(brief_text: str) -> str:
    """Extract seniority level from the compiled brief text."""
    lower = brief_text.lower()
    markers = {
        "staff": ["staff", "principal", "distinguished"],
        "senior": ["senior"],
        "mid": ["mid-level", "mid level", "mid-career", "intermediate"],
        "junior": ["junior", "entry-level", "entry level", "early-career"],
    }
    first_section = lower[:500]
    for level, keywords in markers.items():
        for kw in keywords:
            if kw in first_section:
                return level
    return "mid"


def _parse_gaps(brief_text: str) -> list[StrategyGap]:
    """Extract GAP lines from the compiled brief text."""
    gaps = []
    gap_pattern = re.compile(
        r"GAP:\s*(.+?)\s*\|\s*(OPTIONS:\s*(.+)|FREETEXT)",
        re.IGNORECASE,
    )
    for i, match in enumerate(gap_pattern.finditer(brief_text)):
        question = match.group(1).strip()
        if question.lower() == "none":
            continue
        options_raw = match.group(3)
        options = []
        if options_raw:
            options = [o.strip() for o in options_raw.split(",") if o.strip()]
        gaps.append(StrategyGap(
            id=f"gap_{i}",
            question=question,
            options=options,
        ))
    return gaps


# ---------------------------------------------------------------------------
# Second-Pass Recompilation (with operator gap answers)
# ---------------------------------------------------------------------------

RECOMPILE_PROMPT = """You previously compiled an interview strategy brief. The coach operator has now answered some clarifying questions. Update the strategy brief to incorporate their answers.

ORIGINAL STRATEGY BRIEF:
{original_brief}

OPERATOR ANSWERS TO GAP QUESTIONS:
{answers_block}

Produce an UPDATED strategy brief that:
1. Integrates all operator answers into the relevant sections
2. Adjusts seniority calibration, story selection, or emphasis based on answers
3. Keeps the same structure and density as the original
4. Removes the GAPS section (all gaps are now resolved)

Output ONLY the updated brief text."""


async def recompile_with_answers(
    existing_brief: StrategyBrief,
    gap_answers: dict[str, str],
    context_docs: list[dict],
) -> StrategyBrief:
    """
    Second-pass recompilation incorporating operator answers to gap questions.
    """
    if not gap_answers:
        return existing_brief

    answers_block = "\n".join(
        f"Q: {q}\nA: {a}" for q, a in gap_answers.items() if a.strip()
    )

    if not answers_block.strip():
        return existing_brief

    try:
        client = anthropic.AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)

        response = await client.messages.create(
            model=settings.CLAUDE_MODEL,
            max_tokens=2000,
            system="You are a senior interview coach. Refine the strategy brief with new information.",
            messages=[{
                "role": "user",
                "content": RECOMPILE_PROMPT.format(
                    original_brief=existing_brief.brief_text,
                    answers_block=answers_block,
                ),
            }],
        )

        new_text = response.content[0].text
        seniority = _parse_seniority(new_text)
        spoken_rules = SPOKEN_RULES.get(seniority, SPOKEN_RULES["mid"])

        logger.info(
            f"[Strategy] Recompiled with {len(gap_answers)} answers: "
            f"seniority={seniority}, brief_len={len(new_text)} chars"
        )

        return StrategyBrief(
            seniority_level=seniority,
            round_type=existing_brief.round_type,
            brief_text=new_text,
            spoken_rules=spoken_rules,
            gaps=[],
        )

    except Exception as e:
        logger.error(f"[Strategy] Recompilation failed: {e}")
        return existing_brief
