/**
 * SmadProx v2 — Setup Window Renderer
 *
 * 3-step setup: Select Mic -> Test Audio -> Ready to Go
 * System audio is captured via Electron loopback (getDisplayMedia)
 * — no BlackHole or virtual audio device needed.
 */

function initSetup() {
  const smadprox = window.smadprox;

  // ─── State ────────────────────────────────────────────────────────────────
  let currentStep = 1;
  let devices = [];
  let testAudioConnection = null;
  let liveAudioConnection = null;
  let sysDetectedAudio = false;

  // ─── DOM refs ─────────────────────────────────────────────────────────────
  const $ = id => document.getElementById(id);

  const steps = [1, 2, 3].map(i => $(`step-${i}`));
  const dots = [1, 2, 3].map(i => $(`dot-${i}`));
  const interviewScreen = $('interview-screen');
  const micDeviceSelect = $('mic-device');
  const deviceWarning = $('device-warning');
  const testTranscript = $('test-transcript');
  const sysLevelBar = $('sys-level');
  const micLevelBar = $('mic-level');
  const checkAudio = $('check-audio');
  const checkTranscript = $('check-transcript');
  const candidateIdInput = $('candidate-id');
  const serverUrlInput = $('server-url');
  const checkSys = $('check-sys');
  const checkMic = $('check-mic');
  const checkServer = $('check-server');
  const checkScript = $('check-script');
  const sysNameSpan = $('sys-name');
  const micNameSpan = $('mic-name');
  const btnStart = $('btn-start');
  const startError = $('start-error');
  const liveSysLevel = $('live-sys-level');
  const liveMicLevel = $('live-mic-level');

  // ─── Step Navigation ──────────────────────────────────────────────────────

  function goToStep(n) {
    console.log('[Setup] goToStep(' + n + ') called from:', new Error().stack.split('\n')[2]?.trim());
    currentStep = n;
    steps.forEach((el, i) => { if (el) el.classList.toggle('active', i === n - 1); });
    dots.forEach((el, i) => {
      if (!el) return;
      el.classList.toggle('active', i === n - 1);
      el.classList.toggle('done', i < n - 1);
    });
    if (interviewScreen) interviewScreen.classList.remove('active');

    if (n === 1) enumerateDevices();
    if (n === 2) startTestAudio();
    if (n === 3) runPreflightChecks();
  }

  function showInterviewScreen() {
    steps.forEach(el => { if (el) el.classList.remove('active'); });
    if (interviewScreen) interviewScreen.classList.add('active');
  }

  // ─── Step 1: Select Microphone ────────────────────────────────────────────

  async function enumerateDevices() {
    try {
      await navigator.mediaDevices.getUserMedia({ audio: true });
      devices = await navigator.mediaDevices.enumerateDevices();
      const audioInputs = devices.filter(d => d.kind === 'audioinput' && d.deviceId !== 'default' && d.deviceId !== 'communications');

      micDeviceSelect.innerHTML = '';

      let micId = null;

      audioInputs.forEach(d => {
        const label = d.label || `Device ${d.deviceId.slice(0, 8)}`;
        micDeviceSelect.appendChild(new Option(label, d.deviceId));

        if ((label.toLowerCase().includes('macbook') || label.toLowerCase().includes('built-in')) && !micId) {
          micId = d.deviceId;
        }
      });

      const savedMic = await smadprox.storeGet('micDeviceId');

      if (savedMic && audioInputs.some(d => d.deviceId === savedMic)) micDeviceSelect.value = savedMic;
      else if (micId) micDeviceSelect.value = micId;
    } catch (err) {
      deviceWarning.textContent = 'Could not access audio devices. Grant microphone permission and try again.';
      deviceWarning.style.display = 'block';
    }
  }

  $('btn-step1-next').addEventListener('click', () => {
    smadprox.storeSet('micDeviceId', micDeviceSelect.value);
    goToStep(2);
  });

  // ─── Step 2: Audio Test ───────────────────────────────────────────────────

  $('btn-open-youtube').addEventListener('click', () => {
    smadprox.openExternal('https://www.youtube.com/watch?v=dQw4w9WgXcQ');
  });

  $('btn-step2-back').addEventListener('click', async () => {
    await stopTestAudio();
    goToStep(1);
  });

  $('btn-step2-next').addEventListener('click', async () => {
    await stopTestAudio();
    smadprox.storeSet('setupComplete', true);
    goToStep(3);
  });

  async function startTestAudio() {
    sysDetectedAudio = false;
    testTranscript.textContent = 'Waiting for audio... Play something on your speakers.';
    testTranscript.classList.remove('has-text');
    checkAudio.textContent = '~'; checkAudio.className = 'check-icon wait';
    checkTranscript.textContent = '~'; checkTranscript.className = 'check-icon wait';

    try {
      // Capture mic first, then system audio via loopback — order matters
      const micStream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints(micDeviceSelect.value) });
      const sysStream = await navigator.mediaDevices.getDisplayMedia({ video: false, audio: true });

      const dummyWs = { readyState: 1, send() {} };

      const sysPipeline = startAudioPipeline(sysStream, dummyWs, 'sys', (tag, level) => {
        sysLevelBar.style.width = Math.round(level * 100) + '%';
        if (level > 0.02 && !sysDetectedAudio) {
          sysDetectedAudio = true;
          checkAudio.textContent = '\u2713'; checkAudio.className = 'check-icon ok';
          checkTranscript.textContent = '\u2713'; checkTranscript.className = 'check-icon ok';
          testTranscript.textContent = 'System audio detected! Loopback capture is working.';
          testTranscript.classList.add('has-text');
        }
      });

      const micPipeline = startAudioPipeline(micStream, dummyWs, 'mic', (tag, level) => {
        micLevelBar.style.width = Math.round(level * 100) + '%';
      });

      testAudioConnection = {
        stop() {
          try { sysPipeline.processor.disconnect(); sysPipeline.ctx.close(); } catch (_) {}
          try { micPipeline.processor.disconnect(); micPipeline.ctx.close(); } catch (_) {}
          sysStream.getTracks().forEach(t => t.stop());
          micStream.getTracks().forEach(t => t.stop());
        }
      };
    } catch (err) {
      testTranscript.textContent = 'Error: ' + err.message;
    }
  }

  async function stopTestAudio() {
    if (testAudioConnection) { testAudioConnection.stop(); testAudioConnection = null; }
    sysLevelBar.style.width = '0%';
    micLevelBar.style.width = '0%';
  }

  // ─── Step 3: Pre-flight & Start ───────────────────────────────────────────

  async function runPreflightChecks() {
    const savedCandidateId = await smadprox.storeGet('candidateId');
    const savedServerUrl = await smadprox.storeGet('serverUrl');
    const savedMic = await smadprox.storeGet('micDeviceId');

    if (savedCandidateId) candidateIdInput.value = savedCandidateId;
    if (savedServerUrl) serverUrlInput.value = savedServerUrl;

    // System audio uses loopback — always available
    sysNameSpan.textContent = 'Loopback (automatic)';
    checkSys.textContent = '\u2713'; checkSys.className = 'check-icon ok';

    const micDevice = devices.find(d => d.deviceId === (savedMic || micDeviceSelect.value));

    if (micDevice) {
      micNameSpan.textContent = micDevice.label || 'Selected';
      checkMic.textContent = '\u2713'; checkMic.className = 'check-icon ok';
    } else {
      micNameSpan.textContent = 'Not selected';
      checkMic.textContent = '\u2717'; checkMic.className = 'check-icon fail';
    }

    const serverUrl = serverUrlInput.value || savedServerUrl;
    try {
      const resp = await fetch(serverUrl + '/health', { method: 'GET', signal: AbortSignal.timeout(5000) }).catch(() => null);
      checkServer.textContent = (resp && resp.ok) ? '\u2713' : '?';
      checkServer.className = (resp && resp.ok) ? 'check-icon ok' : 'check-icon wait';
    } catch {
      checkServer.textContent = '?'; checkServer.className = 'check-icon wait';
    }

    checkScript.textContent = '?'; checkScript.className = 'check-icon wait';
    updateStartButton();
  }

  function updateStartButton() {
    btnStart.disabled = !(candidateIdInput.value.trim() && serverUrlInput.value.trim());
  }

  candidateIdInput.addEventListener('input', updateStartButton);
  serverUrlInput.addEventListener('input', updateStartButton);

  btnStart.addEventListener('click', async () => {
    const candidateId = candidateIdInput.value.trim();
    const serverUrl = serverUrlInput.value.trim();
    const micDeviceId = await smadprox.storeGet('micDeviceId') || micDeviceSelect.value;

    if (!candidateId || !serverUrl) {
      startError.textContent = 'Candidate ID and Server URL are required.';
      startError.style.display = 'block';
      return;
    }
    startError.style.display = 'none';

    await smadprox.storeSet('candidateId', candidateId);
    await smadprox.storeSet('serverUrl', serverUrl);

    smadprox.startInterview({ candidateId, serverUrl, micDeviceId });
    showInterviewScreen();

    try {
      liveAudioConnection = await connectAudioStreams({
        micDeviceId, serverUrl, sessionId: candidateId,
        onLevel: (tag, level) => {
          const pct = Math.round(level * 100) + '%';
          if (tag === 'sys') liveSysLevel.style.width = pct;
          else liveMicLevel.style.width = pct;
        },
        onStatus: () => {},
      });
    } catch (err) {
      const sub = document.querySelector('.interview-sub');
      if (sub) sub.textContent = 'Audio not connected: ' + err.message + '. Overlay is still running.';
    }
  });

  $('btn-step3-back').addEventListener('click', () => goToStep(2));

  $('btn-restart').addEventListener('click', async () => {
    // Stop current connection
    if (liveAudioConnection) { liveAudioConnection.stop(); liveAudioConnection = null; }
    smadprox.stopInterview();

    // Brief pause then restart with same settings
    const sub = document.querySelector('.interview-sub');
    if (sub) sub.textContent = 'Restarting...';
    await new Promise(r => setTimeout(r, 1000));

    const candidateId = await smadprox.storeGet('candidateId');
    const serverUrl = await smadprox.storeGet('serverUrl');
    const micDeviceId = await smadprox.storeGet('micDeviceId');

    smadprox.startInterview({ candidateId, serverUrl, micDeviceId });

    try {
      liveAudioConnection = await connectAudioStreams({
        micDeviceId, serverUrl, sessionId: candidateId,
        onLevel: (tag, level) => {
          const pct = Math.round(level * 100) + '%';
          if (tag === 'sys') liveSysLevel.style.width = pct;
          else liveMicLevel.style.width = pct;
        },
        onStatus: () => {},
      });
      if (sub) sub.textContent = 'Overlay is running. Audio is streaming.';
    } catch (err) {
      if (sub) sub.textContent = 'Audio not connected: ' + err.message;
    }
  });

  $('btn-stop').addEventListener('click', () => {
    if (liveAudioConnection) { liveAudioConnection.stop(); liveAudioConnection = null; }
    smadprox.stopInterview();
    goToStep(3);
  });

  // ─── Init ─────────────────────────────────────────────────────────────────

  // Always start at step 1 on fresh launch
  goToStep(1);
}

// Run immediately if DOM ready, otherwise wait
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initSetup);
} else {
  initSetup();
}
