"""
Generate test audio fixtures using ElevenLabs TTS.

Produces PCM16 @ 16kHz mono .raw files that can be injected directly
into the backend's /ws/mac/{session_id} WebSocket to simulate an
interviewer speaking.

Usage:
    cd smadprox-v2
    python tests/fixtures/generate_fixtures.py

Files are written to tests/fixtures/questions/ and .gitignored.
Run once, reuse forever (until you want new test questions).
"""

import os
import sys
import struct
import httpx

# Add backend to path for config
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'backend'))

QUESTIONS = {
    "intro": "Tell me about yourself and your background.",
    "why_company": "Why are you interested in this role at our company?",
    "behavioral_conflict": "Tell me about a time you had a disagreement with a teammate. How did you resolve it?",
    "behavioral_challenge": "Describe the most challenging technical problem you've solved.",
    "system_design": "How would you design a real-time notification system that handles millions of users?",
    "follow_up": "Can you elaborate on that last point?",
    "clarification": "What was the timeline for that project?",
    "coding": "Write a function that finds the longest palindromic substring.",
    "why_leaving": "Why are you looking to leave your current position?",
    "closing": "Do you have any questions for me?",
}

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "questions")
SAMPLE_RATE = 16000


def generate_with_elevenlabs(text: str, output_path: str):
    """Generate PCM16 audio via ElevenLabs and save as raw file."""
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', '..', 'backend', '.env'))

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    voice_id = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")

    if not api_key:
        print("ERROR: ELEVENLABS_API_KEY not set. Set it in backend/.env")
        sys.exit(1)

    resp = httpx.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream",
        headers={"xi-api-key": api_key},
        json={
            "text": text,
            "model_id": "eleven_turbo_v2_5",
            "output_format": "pcm_16000",
        },
        params={"output_format": "pcm_16000"},
        timeout=30,
    )

    if resp.status_code != 200:
        print(f"ERROR: ElevenLabs returned {resp.status_code}: {resp.text[:200]}")
        return False

    with open(output_path, "wb") as f:
        f.write(resp.content)

    duration_sec = len(resp.content) / (SAMPLE_RATE * 2)  # 16-bit = 2 bytes/sample
    print(f"  Generated: {output_path} ({duration_sec:.1f}s, {len(resp.content)} bytes)")
    return True


def generate_silence(duration_sec: float, output_path: str):
    """Generate silence as PCM16 for padding/testing."""
    num_samples = int(SAMPLE_RATE * duration_sec)
    silence = b'\x00\x00' * num_samples
    with open(output_path, "wb") as f:
        f.write(silence)
    print(f"  Generated silence: {output_path} ({duration_sec}s)")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Generate 1 second of silence for padding between questions
    generate_silence(1.0, os.path.join(OUTPUT_DIR, "silence_1s.raw"))

    # Generate each test question
    for name, text in QUESTIONS.items():
        output_path = os.path.join(OUTPUT_DIR, f"{name}.raw")
        if os.path.exists(output_path):
            print(f"  Skipping {name} (already exists)")
            continue

        print(f"Generating: {name}")
        print(f"  Text: {text}")
        if not generate_with_elevenlabs(text, output_path):
            print(f"  FAILED: {name}")

    # Write a manifest
    manifest_path = os.path.join(OUTPUT_DIR, "manifest.txt")
    with open(manifest_path, "w") as f:
        for name, text in QUESTIONS.items():
            path = os.path.join(OUTPUT_DIR, f"{name}.raw")
            exists = os.path.exists(path)
            size = os.path.getsize(path) if exists else 0
            f.write(f"{name}\t{exists}\t{size}\t{text}\n")

    print(f"\nManifest: {manifest_path}")
    print(f"Total fixtures: {len(QUESTIONS)}")


if __name__ == "__main__":
    main()
