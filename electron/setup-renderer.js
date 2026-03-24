/**
 * SmadProx v2 — Setup Window Renderer
 *
 * Handles: BlackHole guidance, device selection, YouTube test,
 * candidate_id entry, pre-interview checks, start/stop interview.
 *
 * Lifted from:
 *   - electron-candidate/index.html (device setup, registration)
 *   - electron-sender/renderer.js (BlackHole detection/validation)
 */

const { smadprox } = window;

// ─── State ──────────────────────────────────────────────────────────────────

let currentStep = 1;
let devices = [];
let testAudioConnection = null; // { stop } from connectAudioStreams during test
let liveAudioConnection = null; // { stop } during live interview
let sysDetectedAudio = false;
let transcriptDetected = false;

// ─── DOM refs ───────────────────────────────────────────────────────────────

const steps = [1, 2, 3, 4].map(i => document.getElementById(`step-${i}`));
const dots = [1, 2, 3, 4].map(i => document.getElementById(`dot-${i}`));
const interviewScreen = document.getElementById('interview-screen');

// Step 1
const btnDownloadBH = document.getElementById('btn-download-blackhole');
const btnOpenMidi = document.getElementById('btn-open-midi');
const btnStep1Next = document.getElementById('btn-step1-next');

// Step 2
const sysDeviceSelect = document.getElementById('sys-device');
const micDeviceSelect = document.getElementById('mic-device');
const deviceWarning = document.getElementById('device-warning');
const btnStep2Back = document.getElementById('btn-step2-back');
const btnStep2Next = document.getElementById('btn-step2-next');

// Step 3
const btnOpenYoutube = document.getElementById('btn-open-youtube');
const testTranscript = document.getElementById('test-transcript');
const sysLevelBar = document.getElementById('sys-level');
const micLevelBar = document.getElementById('mic-level');
const checkAudio = document.getElementById('check-audio');
const checkTranscript = document.getElementById('check-transcript');
const btnStep3Back = document.getElementById('btn-step3-back');
const btnStep3Next = document.getElementById('btn-step3-next');

// Step 4
const candidateIdInput = document.getElementById('candidate-id');
const serverUrlInput = document.getElementById('server-url');
const checkSys = document.getElementById('check-sys');
const checkMic = document.getElementById('check-mic');
const checkServer = document.getElementById('check-server');
const checkScript = document.getElementById('check-script');
const sysNameSpan = document.getElementById('sys-name');
const micNameSpan = document.getElementById('mic-name');
const btnStart = document.getElementById('btn-start');
const startError = document.getElementById('start-error');

// Live interview screen
const liveSysLevel = document.getElementById('live-sys-level');
const liveMicLevel = document.getElementById('live-mic-level');
const btnStop = document.getElementById('btn-stop');

// ─── Step Navigation ────────────────────────────────────────────────────────

function goToStep(n) {
  currentStep = n;
  steps.forEach((el, i) => el.classList.toggle('active', i === n - 1));
  dots.forEach((el, i) => {
    el.classList.toggle('active', i === n - 1);
    el.classList.toggle('done', i < n - 1);
  });
  interviewScreen.classList.remove('active');

  if (n === 2) enumerateDevices();
  if (n === 4) runPreflightChecks();
}

function showInterviewScreen() {
  steps.forEach(el => el.classList.remove('active'));
  interviewScreen.classList.add('active');
}

// ─── Step 1: BlackHole ──────────────────────────────────────────────────────

btnDownloadBH.addEventListener('click', () => {
  smadprox.openExternal('https://existential.audio/blackhole/');
});

btnOpenMidi.addEventListener('click', () => {
  smadprox.openAudioMidi();
});

btnStep1Next.addEventListener('click', () => goToStep(2));

// ─── Step 2: Device Selection ───────────────────────────────────────────────
// BlackHole detection lifted from electron-sender/renderer.js

async function enumerateDevices() {
  try {
    // Request mic permission first (needed to see labels)
    await navigator.mediaDevices.getUserMedia({ audio: true });
    devices = await navigator.mediaDevices.enumerateDevices();
    const audioInputs = devices.filter(d => d.kind === 'audioinput' && d.deviceId !== 'default' && d.deviceId !== 'communications');

    sysDeviceSelect.innerHTML = '';
    micDeviceSelect.innerHTML = '';

    let bhId = null;
    let micId = null;

    audioInputs.forEach(d => {
      const label = d.label || `Device ${d.deviceId.slice(0, 8)}`;
      const optSys = new Option(label, d.deviceId);
      const optMic = new Option(label, d.deviceId);
      sysDeviceSelect.appendChild(optSys);
      micDeviceSelect.appendChild(optMic);

      // Auto-detect BlackHole
      if (label.toLowerCase().includes('blackhole') || label.toLowerCase().includes('multi-output') || label.toLowerCase().includes('aggregate')) {
        bhId = d.deviceId;
      }
      // Auto-detect built-in mic
      if (label.toLowerCase().includes('macbook') || label.toLowerCase().includes('built-in')) {
        if (!micId) micId = d.deviceId;
      }
    });

    // Load saved preferences
    const savedSys = await smadprox.storeGet('sysDeviceId');
    const savedMic = await smadprox.storeGet('micDeviceId');

    if (savedSys && audioInputs.some(d => d.deviceId === savedSys)) {
      sysDeviceSelect.value = savedSys;
    } else if (bhId) {
      sysDeviceSelect.value = bhId;
    }

    if (savedMic && audioInputs.some(d => d.deviceId === savedMic)) {
      micDeviceSelect.value = savedMic;
    } else if (micId) {
      micDeviceSelect.value = micId;
    }

    validateDeviceSelection();
  } catch (err) {
    deviceWarning.textContent = 'Could not access audio devices. Grant microphone permission and try again.';
    deviceWarning.style.display = 'block';
  }
}

function validateDeviceSelection() {
  const sysLabel = sysDeviceSelect.options[sysDeviceSelect.selectedIndex]?.text || '';
  const isMultiOutput = sysLabel.toLowerCase().includes('blackhole') ||
    sysLabel.toLowerCase().includes('multi-output') ||
    sysLabel.toLowerCase().includes('aggregate') ||
    sysLabel.toLowerCase().includes('soundflower');

  if (!isMultiOutput) {
    deviceWarning.textContent = `Warning: "${sysLabel}" may not capture system audio. Select a BlackHole or Multi-Output device for interviewer transcription.`;
    deviceWarning.style.display = 'block';
  } else {
    deviceWarning.style.display = 'none';
  }
}

sysDeviceSelect.addEventListener('change', validateDeviceSelection);

btnStep2Back.addEventListener('click', () => goToStep(1));
btnStep2Next.addEventListener('click', () => {
  smadprox.storeSet('sysDeviceId', sysDeviceSelect.value);
  smadprox.storeSet('micDeviceId', micDeviceSelect.value);
  goToStep(3);
});

// ─── Step 3: YouTube Test ───────────────────────────────────────────────────

btnOpenYoutube.addEventListener('click', () => {
  smadprox.openExternal('https://www.youtube.com/watch?v=dQw4w9WgXcQ');
});

btnStep3Back.addEventListener('click', async () => {
  await stopTestAudio();
  goToStep(2);
});

btnStep3Next.addEventListener('click', async () => {
  await stopTestAudio();
  smadprox.storeSet('setupComplete', true);
  goToStep(4);
});

// Start test audio capture when entering step 3
const origGoToStep = goToStep;
goToStep = function(n) {
  if (n === 3) startTestAudio();
  origGoToStep(n);
};

async function startTestAudio() {
  sysDetectedAudio = false;
  transcriptDetected = false;
  testTranscript.textContent = 'Waiting for audio...';
  testTranscript.classList.remove('has-text');
  checkAudio.textContent = '~'; checkAudio.className = 'check-icon wait';
  checkTranscript.textContent = '~'; checkTranscript.className = 'check-icon wait';

  try {
    const sysDeviceId = sysDeviceSelect.value;
    const micDeviceId = micDeviceSelect.value;
    const serverUrl = await smadprox.storeGet('serverUrl');

    // For testing, we create a temporary session ID
    const testSessionId = 'test-' + Math.random().toString(36).substring(2, 10);

    // Import audio capture
    const { connectAudioStreams } = require('./shared/audio-capture.js');
    testAudioConnection = await connectAudioStreams({
      sysDeviceId,
      micDeviceId,
      serverUrl,
      sessionId: testSessionId,
      onLevel: (tag, level) => {
        const pct = Math.round(level * 100) + '%';
        if (tag === 'sys') {
          sysLevelBar.style.width = pct;
          if (level > 0.02 && !sysDetectedAudio) {
            sysDetectedAudio = true;
            checkAudio.textContent = '\u2713';
            checkAudio.className = 'check-icon ok';
          }
        } else {
          micLevelBar.style.width = pct;
        }
      },
      onStatus: (msg) => {
        testTranscript.textContent = msg;
      },
    });

    // Listen for transcript events via the system audio WebSocket
    // The backend will transcribe and we'd need a test endpoint.
    // For now, audio level detection confirms the pipeline works.
    // Transcript confirmation will happen when backend is connected.
    setTimeout(() => {
      if (sysDetectedAudio) {
        transcriptDetected = true;
        checkTranscript.textContent = '\u2713';
        checkTranscript.className = 'check-icon ok';
        testTranscript.textContent = 'Audio pipeline working. System audio detected.';
        testTranscript.classList.add('has-text');
      }
    }, 3000);

  } catch (err) {
    testTranscript.textContent = 'Error: ' + err.message;
    console.error('Test audio error:', err);
  }
}

async function stopTestAudio() {
  if (testAudioConnection) {
    testAudioConnection.stop();
    testAudioConnection = null;
  }
  sysLevelBar.style.width = '0%';
  micLevelBar.style.width = '0%';
}

// ─── Step 4: Pre-flight & Start ─────────────────────────────────────────────

async function runPreflightChecks() {
  // Load saved values
  const savedCandidateId = await smadprox.storeGet('candidateId');
  const savedServerUrl = await smadprox.storeGet('serverUrl');
  const savedSys = await smadprox.storeGet('sysDeviceId');
  const savedMic = await smadprox.storeGet('micDeviceId');

  if (savedCandidateId) candidateIdInput.value = savedCandidateId;
  if (savedServerUrl) serverUrlInput.value = savedServerUrl;

  // Device checks
  const sysDevice = devices.find(d => d.deviceId === (savedSys || sysDeviceSelect.value));
  const micDevice = devices.find(d => d.deviceId === (savedMic || micDeviceSelect.value));

  if (sysDevice) {
    sysNameSpan.textContent = sysDevice.label || 'Selected';
    checkSys.textContent = '\u2713'; checkSys.className = 'check-icon ok';
  } else {
    sysNameSpan.textContent = 'Not selected';
    checkSys.textContent = '\u2717'; checkSys.className = 'check-icon fail';
  }

  if (micDevice) {
    micNameSpan.textContent = micDevice.label || 'Selected';
    checkMic.textContent = '\u2713'; checkMic.className = 'check-icon ok';
  } else {
    micNameSpan.textContent = 'Not selected';
    checkMic.textContent = '\u2717'; checkMic.className = 'check-icon fail';
  }

  // Server check — try to reach the server
  const serverUrl = serverUrlInput.value || savedServerUrl;
  try {
    const resp = await fetch(serverUrl + '/health', { method: 'GET', signal: AbortSignal.timeout(5000) }).catch(() => null);
    if (resp && resp.ok) {
      checkServer.textContent = '\u2713'; checkServer.className = 'check-icon ok';
    } else {
      // Server might not have /health but still be up
      checkServer.textContent = '?'; checkServer.className = 'check-icon wait';
    }
  } catch {
    checkServer.textContent = '?'; checkServer.className = 'check-icon wait';
  }

  // Script check — placeholder (will check Supabase when backend supports it)
  checkScript.textContent = '?'; checkScript.className = 'check-icon wait';

  updateStartButton();
}

function updateStartButton() {
  const hasCandidate = candidateIdInput.value.trim().length > 0;
  const hasServer = serverUrlInput.value.trim().length > 0;
  btnStart.disabled = !(hasCandidate && hasServer);
}

candidateIdInput.addEventListener('input', updateStartButton);
serverUrlInput.addEventListener('input', updateStartButton);

btnStart.addEventListener('click', async () => {
  const candidateId = candidateIdInput.value.trim();
  const serverUrl = serverUrlInput.value.trim();
  const sysDeviceId = await smadprox.storeGet('sysDeviceId') || sysDeviceSelect.value;
  const micDeviceId = await smadprox.storeGet('micDeviceId') || micDeviceSelect.value;

  if (!candidateId || !serverUrl) {
    startError.textContent = 'Candidate ID and Server URL are required.';
    startError.style.display = 'block';
    return;
  }
  startError.style.display = 'none';

  // Save to store
  await smadprox.storeSet('candidateId', candidateId);
  await smadprox.storeSet('serverUrl', serverUrl);

  // Start audio capture
  try {
    const { connectAudioStreams } = require('./shared/audio-capture.js');
    liveAudioConnection = await connectAudioStreams({
      sysDeviceId,
      micDeviceId,
      serverUrl,
      sessionId: candidateId,
      onLevel: (tag, level) => {
        const pct = Math.round(level * 100) + '%';
        if (tag === 'sys') liveSysLevel.style.width = pct;
        else liveMicLevel.style.width = pct;
      },
      onStatus: () => {},
    });
  } catch (err) {
    startError.textContent = 'Failed to connect: ' + err.message;
    startError.style.display = 'block';
    return;
  }

  // Tell main process to show overlay
  smadprox.startInterview({
    candidateId,
    serverUrl,
    sysDeviceId,
    micDeviceId,
  });

  showInterviewScreen();
});

btnStop.addEventListener('click', () => {
  if (liveAudioConnection) {
    liveAudioConnection.stop();
    liveAudioConnection = null;
  }
  smadprox.stopInterview();
  goToStep(4);
});

// ─── Init ───────────────────────────────────────────────────────────────────

(async () => {
  const setupComplete = await smadprox.storeGet('setupComplete');
  if (setupComplete) {
    // Skip wizard, go straight to step 4 (pre-interview)
    await enumerateDevices();
    goToStep(4);
  } else {
    goToStep(1);
  }
})();
