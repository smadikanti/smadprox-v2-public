/**
 * Shared Audio Capture Module
 *
 * Handles dual audio streams: system audio (loopback) + microphone.
 * System audio is captured via Electron's getDisplayMedia loopback
 * — no BlackHole or virtual audio device needed.
 * Sends PCM16 @ 16kHz in 80ms chunks over WebSocket.
 */

const SAMPLE_RATE = 16000;
const CHUNK_SAMPLES = 1280; // 80ms at 16kHz

function float32ToInt16(f) {
  const out = new Int16Array(f.length);
  for (let i = 0; i < f.length; i++) {
    const s = Math.max(-1, Math.min(1, f[i]));
    out[i] = s < 0 ? s * 0x8000 : s * 0x7FFF;
  }
  return out;
}

/**
 * Start capturing audio from a MediaStream and streaming PCM16 chunks over a WebSocket.
 *
 * @param {MediaStream} stream - getUserMedia stream
 * @param {WebSocket} ws - target WebSocket (must be OPEN)
 * @param {string} tag - 'mic' or 'sys' (used for level callback)
 * @param {function} onLevel - callback(tag, level) where level is 0..1, called per audio frame
 * @returns {{ ctx: AudioContext, processor: ScriptProcessorNode }} for cleanup
 */
function startAudioPipeline(stream, ws, tag, onLevel) {
  const ctx = new AudioContext({ sampleRate: SAMPLE_RATE });
  const source = ctx.createMediaStreamSource(stream);
  const processor = ctx.createScriptProcessor(512, 1, 1);
  let accumulator = new Float32Array(0);
  source.connect(processor);
  processor.connect(ctx.destination);

  processor.onaudioprocess = (e) => {
    const input = e.inputBuffer.getChannelData(0);

    let sum = 0;
    for (let i = 0; i < input.length; i++) sum += input[i] * input[i];
    const rms = Math.sqrt(sum / input.length);
    const level = Math.min(1, Math.pow(rms * 30, 0.7));
    if (onLevel) onLevel(tag, level);

    if (!ws || ws.readyState !== WebSocket.OPEN) return;

    const newBuf = new Float32Array(accumulator.length + input.length);
    newBuf.set(accumulator);
    newBuf.set(input, accumulator.length);
    accumulator = newBuf;

    while (accumulator.length >= CHUNK_SAMPLES) {
      const chunk = accumulator.slice(0, CHUNK_SAMPLES);
      accumulator = accumulator.slice(CHUNK_SAMPLES);
      const int16 = float32ToInt16(chunk);
      ws.send(int16.buffer);
    }
  };

  return { ctx, processor };
}

function audioConstraints(deviceId) {
  return {
    deviceId: { exact: deviceId },
    sampleRate: SAMPLE_RATE,
    channelCount: 1,
    echoCancellation: false,
    noiseSuppression: false,
    autoGainControl: false,
  };
}

/**
 * Open WebSockets and start streaming both audio channels.
 *
 * System audio is captured via getDisplayMedia (Electron loopback) —
 * no BlackHole device ID needed. Mic is captured first via getUserMedia
 * (order matters: getUserMedia before getDisplayMedia avoids Chrome bugs).
 *
 * @param {Object} opts
 * @param {string} opts.micDeviceId - microphone device
 * @param {string} opts.serverUrl - e.g. "https://api.nohuman.live"
 * @param {string} opts.sessionId - candidate_id used as session key
 * @param {function} opts.onLevel - callback(tag, level)
 * @param {function} opts.onStatus - callback(msg)
 * @returns {Promise<{ stop: function }>}
 */
async function connectAudioStreams(opts) {
  const { micDeviceId, serverUrl, sessionId, onLevel, onStatus } = opts;

  // Capture mic FIRST, then system audio — order matters
  const micStream = await navigator.mediaDevices.getUserMedia({ audio: audioConstraints(micDeviceId) });
  const sysStream = await navigator.mediaDevices.getDisplayMedia({ video: false, audio: true });

  const wsProto = serverUrl.startsWith('https') ? 'wss' : 'ws';
  const wsHost = serverUrl.replace(/^https?:\/\//, '');

  const sysWs = new WebSocket(`${wsProto}://${wsHost}/ws/mac/${sessionId}`);
  const micWs = new WebSocket(`${wsProto}://${wsHost}/ws/mic/${sessionId}`);

  await Promise.all([
    new Promise((r, j) => { sysWs.onopen = r; sysWs.onerror = j; }),
    new Promise((r, j) => { micWs.onopen = r; micWs.onerror = j; }),
  ]);

  if (onStatus) onStatus('Connected to server');

  const sysPipeline = startAudioPipeline(sysStream, sysWs, 'sys', onLevel);
  const micPipeline = startAudioPipeline(micStream, micWs, 'mic', onLevel);

  function stop() {
    for (const p of [sysPipeline, micPipeline]) {
      try { p.processor.disconnect(); p.ctx.close(); } catch (_) {}
    }
    for (const ws of [sysWs, micWs]) {
      try { ws.send(JSON.stringify({ type: 'stop' })); ws.close(); } catch (_) {}
    }
    for (const s of [sysStream, micStream]) {
      s.getTracks().forEach(t => t.stop());
    }
  }

  return { stop, sysWs, micWs };
}

// Export for both module and script-tag usage
if (typeof module !== 'undefined' && module.exports) {
  module.exports = { SAMPLE_RATE, CHUNK_SAMPLES, float32ToInt16, startAudioPipeline, connectAudioStreams, audioConstraints };
}
