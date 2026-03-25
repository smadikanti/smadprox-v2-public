"""
SmadProx Test Harness — spins up backend + Electron, runs tests, tears down.

Usage:
    # Fast unit tests only (~2 seconds)
    python tests/harness.py unit

    # Full E2E suite (~3 minutes, costs ~$2 in API calls)
    python tests/harness.py e2e

    # Everything
    python tests/harness.py all

    # Generate audio fixtures first (one-time, ~$0.50)
    python tests/harness.py fixtures
"""

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND_DIR = os.path.join(ROOT, "backend")
ELECTRON_DIR = os.path.join(ROOT, "electron")
TESTS_DIR = os.path.join(ROOT, "tests")


def run_fixtures():
    """Generate ElevenLabs audio fixtures."""
    print("\n=== Generating Audio Fixtures ===\n")
    subprocess.run(
        [sys.executable, os.path.join(TESTS_DIR, "fixtures", "generate_fixtures.py")],
        cwd=ROOT,
    )


def run_unit_tests():
    """Run fast unit tests (no server needed)."""
    print("\n=== Running Unit Tests ===\n")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/unit/", "-v", "-m", "unit", "--tb=short"],
        cwd=ROOT,
    )
    return result.returncode


def run_e2e_tests():
    """Run full E2E tests (starts backend + Electron)."""
    print("\n=== Running E2E Tests ===\n")

    # Check if fixtures exist
    fixtures_dir = os.path.join(TESTS_DIR, "fixtures", "questions")
    if not os.path.exists(fixtures_dir) or len(os.listdir(fixtures_dir)) < 3:
        print("Audio fixtures not found. Generating...")
        run_fixtures()

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/e2e/", "-v", "-m", "e2e", "--tb=short", "-x"],
        cwd=ROOT,
    )
    return result.returncode


def run_all():
    """Run everything."""
    unit_rc = run_unit_tests()
    if unit_rc != 0:
        print("\n!!! Unit tests failed — skipping E2E !!!")
        return unit_rc
    return run_e2e_tests()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "fixtures":
        run_fixtures()
    elif cmd == "unit":
        sys.exit(run_unit_tests())
    elif cmd == "e2e":
        sys.exit(run_e2e_tests())
    elif cmd == "all":
        sys.exit(run_all())
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
