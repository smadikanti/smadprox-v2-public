/**
 * SmadProx v2 — Overlay Window Renderer
 *
 * Scrolling feed with three card types: AI, operator, filler.
 * Cards accumulate (never replace). Auto-scroll keeps current card in reading zone.
 *
 * Lifted from reverse-engineer-littlebird/electron/renderer.js, then modified:
 *   - Replaced Pusher with WebSocket to /ws/overlay/{candidate_id}
 *   - Changed from single-card highlight to scrolling feed
 *   - Added operator card type (orange, growing in real-time)
 *   - Added filler card type (dashed, with instruction + progress)
 *   - Added card_demote handler (filler de-emphasis)
 *   - Added cross-fade for card_update text changes
 */

// ─── State ──────────────────────────────────────────────────────────────────

let ws = null;
let candidateId = null;
let serverUrl = null;
let autoScroll = true;
let speed = 4;
let isPaused = false;
let wsConnected = false;
let reconnectDelay = 1000;
let reconnectTimer = null;
let pingInterval = null;
let cardCount = 0;

// ─── DOM ────────────────────────────────────────────────────────────────────

const feedScroll = document.getElementById('feed-scroll');
const feedCards = document.getElementById('feed-cards');
const connectionDot = document.getElementById('connection-dot');
const modeLabel = document.getElementById('mode-label');
const scrollTag = document.getElementById('scroll-tag');
const opacityTag = document.getElementById('opacity-tag');
const speedBtns = document.querySelectorAll('.speed-btn');

// ─── Bottom spacer (pushes last card to reading position) ───────────────────

const bottomSpacer = document.createElement('div');
bottomSpacer.id = 'bottom-spacer';
feedCards.appendChild(bottomSpacer);

function updateBottomSpacer() {
  const h = feedScroll.clientHeight;
  bottomSpacer.style.height = Math.max(h - 120, 60) + 'px';
  bottomSpacer.style.flexShrink = '0';
}
updateBottomSpacer();
window.addEventListener('resize', updateBottomSpacer);
new ResizeObserver(updateBottomSpacer).observe(feedScroll);

// ─── Card Rendering ─────────────────────────────────────────────────────────

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

/**
 * Create a card DOM element.
 * Types: 'ai' | 'operator' | 'operator_live' | 'filler'
 */
function createCardEl(data) {
  const card = document.createElement('div');
  const type = data.is_question ? 'question' : data.is_filler ? 'filler' : data.is_operator ? 'operator' : 'ai';
  const isLive = data.is_operator && !data.is_final;

  card.className = `feed-card ${type}`;
  if (isLive) card.classList.add('live');
  if (data.is_whiteboard) card.classList.add('whiteboard');
  card.dataset.cardId = data.card_id || '';
  card.dataset.type = type;

  // Header
  const header = document.createElement('div');
  header.className = 'card-header';

  const dot = document.createElement('span');
  dot.className = 'card-status-dot';

  const label = document.createElement('span');
  label.className = 'card-label';
  if (type === 'ai') {
    dot.classList.add('ai');
    const idx = data.index != null ? data.index : '';
    const total = data.total || '';
    label.textContent = total ? `${idx}/${total}` : (data.is_whiteboard ? 'Type this' : '');
  } else if (type === 'operator') {
    dot.classList.add('operator');
    label.textContent = isLive ? 'Live' : 'Operator';
  } else if (type === 'filler') {
    dot.classList.add('filler');
    label.textContent = 'Bridge';
  } else if (type === 'question') {
    dot.classList.add('question');
    label.textContent = 'Question';
  }

  header.appendChild(dot);
  header.appendChild(label);

  // Body
  const body = document.createElement('div');
  body.className = 'card-body';
  body.innerHTML = escapeHtml(data.text || '');
  if (isLive) {
    const cursor = document.createElement('span');
    cursor.className = 'typing-cursor';
    cursor.textContent = '\u258C';
    body.appendChild(cursor);
  }

  card.appendChild(header);
  card.appendChild(body);

  // Filler instruction
  if (type === 'filler' && data.instruction) {
    const inst = document.createElement('div');
    inst.className = 'card-instruction';
    inst.textContent = data.instruction;
    card.appendChild(inst);
  }

  // Filler progress bar
  if (type === 'filler' && data.estimated_seconds) {
    const progress = document.createElement('div');
    progress.className = 'card-progress';
    const bar = document.createElement('div');
    bar.className = 'card-progress-fill';
    bar.style.animationDuration = data.estimated_seconds + 's';
    progress.appendChild(bar);
    card.appendChild(progress);
  }

  return card;
}

/**
 * All cards stay at full opacity — candidate can read any card.
 * Only demoted fillers get reduced opacity.
 * The .current card gets a subtle highlight border to show what's newest.
 */
function refreshCardDimming() {
  // No dimming — all cards stay fully visible
}

function addCard(data) {
  cardCount++;
  const el = createCardEl(data);
  feedCards.insertBefore(el, bottomSpacer);

  // New card becomes current — previous current gets dimmed
  feedCards.querySelectorAll('.feed-card.current').forEach(c => c.classList.remove('current'));
  el.classList.add('current');
  refreshCardDimming();

  // Scroll to new card
  if (autoScroll && !isPaused) {
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
}

function updateCard(cardId, text) {
  const card = feedCards.querySelector(`.feed-card[data-card-id="${cardId}"]`);
  if (!card) return;

  const body = card.querySelector('.card-body');
  if (!body) return;

  // Update content instantly for live streaming cards (no fade delay).
  // The 150ms fade was causing flicker with 50ms token streams.
  const isLive = card.classList.contains('live') || card.classList.contains('current');
  body.innerHTML = escapeHtml(text);
  if (isLive && card.classList.contains('live')) {
    const cursor = document.createElement('span');
    cursor.className = 'typing-cursor';
    cursor.textContent = '\u258C';
    body.appendChild(cursor);
  }
}

function highlightCard(cardId) {
  // Remove current highlight from all
  feedCards.querySelectorAll('.feed-card.current').forEach(c => c.classList.remove('current'));
  // Add to target
  const card = feedCards.querySelector(`.feed-card[data-card-id="${cardId}"]`);
  if (card) {
    card.classList.add('current');
  }
  // Re-apply dimming so old cards get .previous and current loses it
  refreshCardDimming();
  if (card && autoScroll) card.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function demoteCard(cardId) {
  const card = feedCards.querySelector(`.feed-card[data-card-id="${cardId}"]`);
  if (!card) return;
  card.classList.remove('previous');   // demoted takes priority over previous
  card.classList.remove('current');    // demoted card is never the current one
  card.classList.add('demoted');
  // Update label
  const label = card.querySelector('.card-label');
  if (label) label.textContent = '(bridge \u2014 answer below)';
  refreshCardDimming();
}

function clearCards() {
  feedCards.innerHTML = '';
  feedCards.appendChild(bottomSpacer);
  cardCount = 0;
}

// ─── WebSocket to backend overlay endpoint ──────────────────────────────────

function connectOverlay() {
  if (!candidateId || !serverUrl) return;
  if (ws) { try { ws.close(); } catch (_) {} }

  const wsProto = serverUrl.startsWith('https') ? 'wss' : 'ws';
  const wsHost = serverUrl.replace(/^https?:\/\//, '').replace(/\/+$/, '');
  const url = `${wsProto}://${wsHost}/ws/overlay/${candidateId}`;

  ws = new WebSocket(url);

  ws.onopen = () => {
    wsConnected = true;
    reconnectDelay = 1000;
    connectionDot.classList.add('connected');
    modeLabel.textContent = isPaused ? 'Paused' : 'Live';
    // Ping every 25s
    clearInterval(pingInterval);
    pingInterval = setInterval(() => {
      if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'ping' }));
      }
    }, 25000);
  };

  ws.onmessage = (evt) => {
    let data;
    try { data = JSON.parse(evt.data); } catch { return; }
    handleMessage(data);
  };

  ws.onclose = () => {
    wsConnected = false;
    connectionDot.classList.remove('connected');
    clearInterval(pingInterval);
    if (!isPaused) scheduleReconnect();
  };

  ws.onerror = () => {
    wsConnected = false;
    connectionDot.classList.remove('connected');
  };
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    reconnectDelay = Math.min(reconnectDelay * 2, 16000);
    connectOverlay();
  }, reconnectDelay);
}

function handleMessage(data) {
  switch (data.type) {
    case 'card_push':
      addCard(data);
      break;

    case 'card_update':
      updateCard(data.card_id, data.text);
      break;

    case 'card_clear':
      clearCards();
      break;

    case 'card_highlight':
      highlightCard(data.card_id);
      break;

    case 'card_demote':
      demoteCard(data.card_id);
      break;

    case 'operator_card':
      // Operator speech card — either new or update existing live card
      const existing = feedCards.querySelector(`.feed-card.operator.live[data-card-id="${data.card_id}"]`);
      if (existing) {
        updateCard(data.card_id, data.text);
        if (data.is_final) {
          existing.classList.remove('live');
          existing.querySelector('.card-label').textContent = 'Operator';
          const cursor = existing.querySelector('.typing-cursor');
          if (cursor) cursor.remove();
        }
      } else {
        addCard({ ...data, is_operator: true });
      }
      break;

    case 'status':
      // Status update from backend
      break;

    case 'pong':
      break;

    default:
      console.log('[overlay] Unknown message type:', data.type);
  }
}

// ─── Auto-scroll loop ──────────────────────────────────────────────────────
// Lifted from reverse-engineer-littlebird/electron/renderer.js

const SPEED_MAP = [10, 20, 30, 45, 60];
let isUserScrolling = false;
let userScrollTimeout = null;
let lastFrameTime = 0;

feedScroll.addEventListener('wheel', () => {
  if (!autoScroll) return;
  isUserScrolling = true;
  clearTimeout(userScrollTimeout);
  userScrollTimeout = setTimeout(() => { isUserScrolling = false; }, 1000);
}, { passive: true });

(function startAutoScrollLoop() {
  function animate(now) {
    if (!lastFrameTime) { lastFrameTime = now; requestAnimationFrame(animate); return; }
    const dt = now - lastFrameTime;
    lastFrameTime = now;

    if (!autoScroll || isUserScrolling || isPaused) {
      requestAnimationFrame(animate);
      return;
    }

    // Check if current card is already in reading zone (top 15%)
    const currentCard = feedCards.querySelector('.feed-card.current');
    if (currentCard) {
      const rect = currentCard.getBoundingClientRect();
      const containerRect = feedScroll.getBoundingClientRect();
      if (rect.top - containerRect.top <= containerRect.height * 0.15) {
        requestAnimationFrame(animate);
        return;
      }
    }

    const pxPerSec = SPEED_MAP[speed - 1] || 30;
    feedScroll.scrollTop += (pxPerSec * dt) / 1000;
    requestAnimationFrame(animate);
  }
  requestAnimationFrame(animate);
})();

// ─── Speed UI ───────────────────────────────────────────────────────────────

function updateSpeedUI(newSpeed) {
  speed = newSpeed;
  speedBtns.forEach(btn => btn.classList.toggle('active', parseInt(btn.dataset.speed) === speed));
}
speedBtns.forEach(btn => btn.addEventListener('click', () => updateSpeedUI(parseInt(btn.dataset.speed))));

function updateScrollUI() {
  scrollTag.textContent = autoScroll ? 'Scroll ON' : 'Scroll OFF';
}

// ─── IPC from main process ──────────────────────────────────────────────────

if (window.noscreen) {
  window.noscreen.onSpeedChanged(s => updateSpeedUI(s));
  window.noscreen.onToggleAutoscroll(() => { autoScroll = !autoScroll; updateScrollUI(); });
  window.noscreen.onOpacityChanged(pct => { opacityTag.textContent = `${pct}%`; });
  window.noscreen.onInteractiveChanged(interactive => {
    document.body.classList.toggle('interactive', interactive);
    modeLabel.textContent = interactive ? 'Interactive' : (isPaused ? 'Paused' : (wsConnected ? 'Live' : 'Connecting'));
    modeLabel.classList.toggle('interactive', interactive);
  });
  window.noscreen.onTogglePause(() => {
    isPaused = !isPaused;
    modeLabel.textContent = isPaused ? 'Paused' : (wsConnected ? 'Live' : 'Connecting');
    if (isPaused) {
      if (ws) ws.close();
      clearTimeout(reconnectTimer);
    } else {
      connectOverlay();
    }
  });
  window.noscreen.onSessionConfig((config) => {
    candidateId = config.candidateId;
    serverUrl = config.serverUrl;
    connectOverlay();
  });
  window.noscreen.onStopSession(() => {
    isPaused = true;
    if (ws) ws.close();
    clearTimeout(reconnectTimer);
    clearInterval(pingInterval);
    clearCards();
    modeLabel.textContent = 'Stopped';
  });
}

// ─── Init ───────────────────────────────────────────────────────────────────

updateSpeedUI(4);
updateScrollUI();
