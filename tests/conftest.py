"""
Shared pytest fixtures for SmadProx v2 tests.

- backend_server: starts FastAPI backend, yields base URL, kills on teardown
- electron_app: starts Electron with --remote-debugging-port, yields DevTools URL
- ws_session: creates a test session with WebSocket connections to mac/mic/dashboard/overlay
- audio_fixture: loads a PCM16 audio file from fixtures/questions/
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time

import httpx
import pytest
import websockets

# Paths
BACKEND_DIR = os.path.join(os.path.dirname(__file__), '..', 'backend')
ELECTRON_DIR = os.path.join(os.path.dirname(__file__), '..', 'electron')
FIXTURES_DIR = os.path.join(os.path.dirname(__file__), 'fixtures', 'questions')

BACKEND_URL = os.environ.get("TEST_BACKEND_URL", "http://localhost:8000")
BACKEND_WS = BACKEND_URL.replace("http://", "ws://")
DEVTOOLS_PORT = 9333  # Different from dev port 9222


# ─── Markers ─────────────────────────────────────────────────────────────────

def pytest_configure(config):
    config.addinivalue_line("markers", "e2e: end-to-end tests (slow, costs money)")
    config.addinivalue_line("markers", "unit: fast unit tests")


# ─── Backend Server ──────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def backend_server():
    """Use existing backend or start one for testing."""
    # Check if backend is already running
    try:
        resp = httpx.get(f"{BACKEND_URL}/health", timeout=2)
        if resp.status_code == 200:
            print(f"\n[conftest] Using existing backend at {BACKEND_URL}")
            yield BACKEND_URL
            return
    except Exception:
        pass

    # Start a new backend
    print(f"\n[conftest] Starting backend at {BACKEND_URL}")
    port = BACKEND_URL.split(":")[-1].rstrip("/")
    env = os.environ.copy()
    env["PORT"] = port
    env["HOST"] = "0.0.0.0"

    proc = subprocess.Popen(
        [sys.executable, "run.py"],
        cwd=BACKEND_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    for _ in range(30):
        try:
            resp = httpx.get(f"{BACKEND_URL}/health", timeout=2)
            if resp.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        proc.kill()
        raise RuntimeError(f"Backend failed to start at {BACKEND_URL}")

    yield BACKEND_URL

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


# ─── Electron App ────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def electron_app():
    """Start Electron app with remote debugging for Chrome DevTools MCP."""
    electron_bin = os.path.join(ELECTRON_DIR, "node_modules", "electron", "dist", "Electron.app")

    proc = subprocess.Popen(
        ["open", electron_bin, "--args", ELECTRON_DIR, f"--remote-debugging-port={DEVTOOLS_PORT}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    # Wait for DevTools to be available
    for _ in range(20):
        try:
            resp = httpx.get(f"http://127.0.0.1:{DEVTOOLS_PORT}/json/version", timeout=2)
            if resp.status_code == 200:
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        raise RuntimeError("Electron app failed to start with DevTools")

    yield f"http://127.0.0.1:{DEVTOOLS_PORT}"

    # Teardown
    subprocess.run(["pkill", "-f", "Electron"], capture_output=True)


# ─── WebSocket Test Session ──────────────────────────────────────────────────

class TestSession:
    """Manages WebSocket connections for a test session."""

    def __init__(self, session_id: str, base_ws: str):
        self.session_id = session_id
        self.base_ws = base_ws
        self.mac_ws = None
        self.mic_ws = None
        self.dashboard_ws = None
        self.overlay_ws = None
        self.dashboard_messages = []
        self.overlay_messages = []
        self._dashboard_task = None
        self._overlay_task = None

    async def connect(self):
        """Open all 4 WebSocket connections."""
        self.mac_ws = await websockets.connect(f"{self.base_ws}/ws/mac/{self.session_id}")
        self.mic_ws = await websockets.connect(f"{self.base_ws}/ws/mic/{self.session_id}")
        self.dashboard_ws = await websockets.connect(f"{self.base_ws}/ws/dashboard/{self.session_id}")
        self.overlay_ws = await websockets.connect(f"{self.base_ws}/ws/overlay/{self.session_id}")

        # Start listening on dashboard and overlay
        self._dashboard_task = asyncio.create_task(self._listen(self.dashboard_ws, self.dashboard_messages))
        self._overlay_task = asyncio.create_task(self._listen(self.overlay_ws, self.overlay_messages))

    async def _listen(self, ws, message_list):
        try:
            async for msg in ws:
                try:
                    data = json.loads(msg)
                    message_list.append(data)
                except json.JSONDecodeError:
                    pass
        except websockets.exceptions.ConnectionClosed:
            pass

    async def send_audio(self, pcm_data: bytes, chunk_size: int = 2560, delay: float = 0.08):
        """Send PCM16 audio in chunks to simulate real-time streaming."""
        for i in range(0, len(pcm_data), chunk_size):
            chunk = pcm_data[i:i + chunk_size]
            await self.mac_ws.send(chunk)
            await asyncio.sleep(delay)

    async def send_mic_audio(self, pcm_data: bytes, chunk_size: int = 2560, delay: float = 0.08):
        """Send PCM16 audio to mic channel."""
        for i in range(0, len(pcm_data), chunk_size):
            chunk = pcm_data[i:i + chunk_size]
            await self.mic_ws.send(chunk)
            await asyncio.sleep(delay)

    async def wait_for_message(self, msg_type: str, source: str = "dashboard", timeout: float = 30.0) -> dict:
        """Wait for a specific message type to appear."""
        messages = self.dashboard_messages if source == "dashboard" else self.overlay_messages
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            for msg in messages:
                if msg.get("type") == msg_type:
                    return msg
            await asyncio.sleep(0.1)
        raise TimeoutError(f"Timed out waiting for {msg_type} on {source} after {timeout}s")

    async def wait_for_messages(self, msg_type: str, count: int = 1, source: str = "dashboard", timeout: float = 30.0) -> list[dict]:
        """Wait for N messages of a specific type."""
        messages = self.dashboard_messages if source == "dashboard" else self.overlay_messages
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            matches = [m for m in messages if m.get("type") == msg_type]
            if len(matches) >= count:
                return matches[:count]
            await asyncio.sleep(0.1)
        matches = [m for m in messages if m.get("type") == msg_type]
        raise TimeoutError(f"Got {len(matches)}/{count} {msg_type} messages in {timeout}s")

    async def close(self):
        """Close all connections."""
        if self._dashboard_task:
            self._dashboard_task.cancel()
        if self._overlay_task:
            self._overlay_task.cancel()
        for ws in [self.mac_ws, self.mic_ws, self.dashboard_ws, self.overlay_ws]:
            if ws:
                await ws.close()


@pytest.fixture
def session_id():
    """Generate a unique session ID for each test."""
    import uuid
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def ws_session(backend_server, session_id):
    """Create a connected test session with all 4 WebSocket channels."""
    base_ws = backend_server.replace("http://", "ws://")
    session = TestSession(session_id, base_ws)
    await session.connect()
    yield session
    await session.close()


# ─── Audio Fixtures ──────────────────────────────────────────────────────────

@pytest.fixture
def audio_fixture():
    """Load a PCM16 audio file from fixtures/questions/."""
    def _load(name: str) -> bytes:
        path = os.path.join(FIXTURES_DIR, f"{name}.raw")
        if not os.path.exists(path):
            pytest.skip(f"Audio fixture not found: {path}. Run: python tests/fixtures/generate_fixtures.py")
        with open(path, "rb") as f:
            return f.read()
    return _load


# ─── Metrics Reader ──────────────────────────────────────────────────────────

@pytest.fixture
def metrics_reader():
    """Read the latest metrics from the JSONL file."""
    def _read(last_n: int = 1) -> list[dict]:
        path = "/tmp/smadprox-metrics.jsonl"
        if not os.path.exists(path):
            return []
        with open(path) as f:
            lines = f.readlines()
        return [json.loads(line) for line in lines[-last_n:]]
    return _read
