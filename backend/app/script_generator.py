"""
Script Generator — Replaces Cursor for generating script.md files.

Triggered by POST /api/generate-script/{submission_id}
Assembles prompts from on-disk templates + Supabase submission data,
calls Claude Opus API, stores result.
"""

import os
import re
import hashlib
import asyncio
from pathlib import Path
from anthropic import AsyncAnthropic

# Prompt templates directory
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"

def load_prompt_file(filename: str) -> str:
    """Load a prompt template from the prompts/ directory."""
    path = PROMPTS_DIR / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""

def match_culture_notes(company: str, culture_text: str) -> str:
    """Extract the section for a specific company from culture-notes.md.
    Culture notes has company sections with ## headers."""
    if not company or not culture_text:
        return ""
    # Search for ## Company Name section
    pattern = re.compile(
        rf"^##\s+.*{re.escape(company)}.*$",
        re.IGNORECASE | re.MULTILINE
    )
    match = pattern.search(culture_text)
    if not match:
        return ""
    start = match.start()
    # Find next ## or end of file
    next_section = re.search(r"^##\s+", culture_text[match.end():], re.MULTILINE)
    if next_section:
        end = match.end() + next_section.start()
    else:
        end = len(culture_text)
    return culture_text[start:end].strip()

def compute_prompt_hash(submission_data: dict, templates: list[str]) -> str:
    """Hash inputs so we can detect if regeneration is needed."""
    content = str(submission_data) + "".join(templates)
    return hashlib.sha256(content.encode()).hexdigest()[:16]

async def generate_script(
    submission: dict,
    prior_scripts: list[str] = None,
    on_status = None,
) -> str:
    """
    Generate a full script.md from submission data.

    Args:
        submission: dict with keys: resume, jd, interviewer_info, recruiter_notes,
                    company, round_type, candidate_name, etc.
        prior_scripts: list of previous script.md contents for this candidate
        on_status: optional callback(status_msg) for progress updates

    Returns:
        The generated script.md content as a string.
    """
    if on_status:
        on_status("Loading prompt templates...")

    # Load all prompt templates
    elaborate_prompt = load_prompt_file("elaborate-script-prompt.md")
    coach_rules = load_prompt_file("interview-coach-rules.md")
    coaching_lessons = load_prompt_file("coaching-lessons.md")
    culture_notes_full = load_prompt_file("culture-notes.md")
    system_design_ref = load_prompt_file("system-design-prompt.md")

    # Match culture notes for this company
    company = submission.get("company", "")
    culture_section = match_culture_notes(company, culture_notes_full)

    # Build system prompt
    system_prompt_parts = [elaborate_prompt]
    if coach_rules:
        system_prompt_parts.append("\n\n---\n\n# Coaching Rules (always apply)\n\n" + coach_rules)
    if coaching_lessons:
        system_prompt_parts.append("\n\n---\n\n# Coaching Lessons (accumulated)\n\n" + coaching_lessons)

    system_prompt = "\n".join(system_prompt_parts)

    # Build user message with submission data
    user_parts = []

    if submission.get("resume"):
        user_parts.append(f"## Resume\n\n{submission['resume']}")
    if submission.get("jd"):
        user_parts.append(f"## Job Description\n\n{submission['jd']}")
    if submission.get("interviewer_info"):
        user_parts.append(f"## Interviewer Info\n\n{submission['interviewer_info']}")
    if submission.get("recruiter_notes"):
        user_parts.append(f"## Recruiter Notes\n\n{submission['recruiter_notes']}")
    if submission.get("round_type"):
        user_parts.append(f"## Round Type\n\n{submission['round_type']}")
    if submission.get("slot_time"):
        user_parts.append(f"## Interview Slot\n\n{submission['slot_time']}")
    if culture_section:
        user_parts.append(f"## Company Culture Notes\n\n{culture_section}")
    if system_design_ref and submission.get("round_type", "").lower() in ("system_design", "system design"):
        user_parts.append(f"## System Design Reference\n\n{system_design_ref}")
    if prior_scripts:
        for i, script in enumerate(prior_scripts):
            # Truncate if very long to save context
            truncated = script[:8000] if len(script) > 8000 else script
            user_parts.append(f"## Prior Round {i+1} Script (for continuity)\n\n{truncated}")

    user_message = "\n\n---\n\n".join(user_parts)

    if on_status:
        on_status("Generating script with Claude Opus...")

    # Call Claude Opus API
    client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    # Use explicit block-level cache_control for system prompt caching
    system_blocks = [
        {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
    ]

    response = await client.messages.create(
        model="claude-opus-4-6",
        max_tokens=16000,
        system=system_blocks,
        messages=[{"role": "user", "content": user_message}],
    )

    script_content = response.content[0].text

    if on_status:
        on_status("Script generated successfully")

    return script_content


async def generate_and_store_script(
    supabase_client,
    submission_id: str,
    on_status = None,
) -> dict:
    """
    Full pipeline: read submission, generate script, store in Supabase.

    Args:
        supabase_client: the app's Supabase client module
        submission_id: UUID of the submission row
        on_status: optional callback

    Returns:
        dict with script_id, status, candidate_id
    """
    if on_status:
        on_status("Reading submission...")

    # Read submission from Supabase
    submission = await supabase_client.get_submission(submission_id)
    if not submission:
        raise ValueError(f"Submission {submission_id} not found")

    candidate_id = submission.get("candidate_id", "")

    # Check for prior scripts
    prior_scripts = await supabase_client.get_prior_scripts(candidate_id)
    prior_contents = [s["script_content"] for s in prior_scripts if s.get("script_content")]

    # Compute hash to check if regeneration is needed
    templates = [
        load_prompt_file("elaborate-script-prompt.md"),
        load_prompt_file("interview-coach-rules.md"),
    ]
    prompt_hash = compute_prompt_hash(submission, templates)

    # Generate
    script_content = await generate_script(
        submission=submission,
        prior_scripts=prior_contents,
        on_status=on_status,
    )

    # Store in Supabase
    script_record = await supabase_client.store_script(
        candidate_id=candidate_id,
        submission_id=submission_id,
        company=submission.get("company", ""),
        round_type=submission.get("round_type", ""),
        script_content=script_content,
        prompt_hash=prompt_hash,
    )

    return {
        "script_id": script_record.get("id"),
        "candidate_id": candidate_id,
        "status": "ready",
    }
