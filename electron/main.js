/**
 * SmadProx v2 — Combined Electron App (main process)
 *
 * Merged from:
 *   - reverse-engineer-littlebird/electron/main.js (overlay window, keyboard shortcuts)
 *   - over-phone-smadprox/electron-candidate/main.js (setup window)
 *
 * Two windows:
 *   1. setupWindow — normal visible window for config, BlackHole setup, YouTube test
 *   2. overlayWindow — invisible overlay for coaching cards during interview
 */

const { app, BrowserWindow, screen, globalShortcut, ipcMain, shell } = require('electron');
const path = require('path');
const Store = require('electron-store');

const store = new Store({
  defaults: {
    candidateId: '',
    serverUrl: 'https://api.nohuman.live',
    sysDeviceId: '',
    micDeviceId: '',
    setupComplete: false,
  },
});

let setupWindow = null;
let overlayWindow = null;

// ─── Setup Window ───────────────────────────────────────────────────────────

function createSetupWindow() {
  const { workArea } = screen.getPrimaryDisplay();

  setupWindow = new BrowserWindow({
    width: 520,
    height: 680,
    x: Math.round(workArea.x + (workArea.width - 520) / 2),
    y: Math.round(workArea.y + (workArea.height - 680) / 3),
    frame: true,
    resizable: true,
    show: false,
    title: 'SmadProx',
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload-setup.js'),
    },
  });

  setupWindow.loadFile('setup.html');
  setupWindow.once('ready-to-show', () => setupWindow.show());
  setupWindow.on('closed', () => { setupWindow = null; });
}

// ─── Overlay Window ─────────────────────────────────────────────────────────
// Lifted from reverse-engineer-littlebird/electron/main.js

function createOverlayWindow() {
  const winW = 480;
  const winH = 750;
  const { workArea } = screen.getPrimaryDisplay();
  const x = Math.round(workArea.x + workArea.width - winW - 20);
  const y = Math.round(workArea.y + (workArea.height - winH) / 3);

  overlayWindow = new BrowserWindow({
    width: winW,
    height: winH,
    x,
    y,
    minWidth: 280,
    minHeight: 200,
    frame: false,
    transparent: true,
    resizable: true,
    roundedCorners: true,
    hasShadow: true,
    backgroundColor: '#00000000',
    show: false,

    alwaysOnTop: true,
    visibleOnAllWorkspaces: true,
    skipTaskbar: true,

    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      preload: path.join(__dirname, 'preload-overlay.js'),
    },
  });

  // Invisible to screenshare
  overlayWindow.setContentProtection(true);
  overlayWindow.setAlwaysOnTop(true, 'screen-saver');
  overlayWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  // Start in click-through mode
  overlayWindow._clickThrough = true;
  overlayWindow.setIgnoreMouseEvents(true, { forward: true });

  overlayWindow.loadFile('overlay.html');

  overlayWindow.once('ready-to-show', () => {
    overlayWindow.webContents.setZoomFactor(0.8);
  });

  overlayWindow.on('moved', () => snapOverlayToScreen());
  overlayWindow.on('closed', () => { overlayWindow = null; });
}

function snapOverlayToScreen() {
  if (!overlayWindow || overlayWindow.isDestroyed()) return;
  const [wx, wy] = overlayWindow.getPosition();
  const [ww, wh] = overlayWindow.getSize();
  const display = screen.getDisplayNearestPoint({ x: wx, y: wy });
  const { x: sx, y: sy, width: sw, height: sh } = display.workArea;

  let nx = Math.max(sx, Math.min(wx, sx + sw - ww));
  let ny = Math.max(sy, Math.min(wy, sy + sh - wh));
  if (nx !== wx || ny !== wy) overlayWindow.setPosition(nx, ny);
}

// ─── IPC: Setup <-> Main ────────────────────────────────────────────────────

ipcMain.handle('store-get', (_e, key) => store.get(key));
ipcMain.handle('store-set', (_e, key, val) => store.set(key, val));
ipcMain.handle('store-all', () => store.store);

ipcMain.handle('open-external', (_e, url) => shell.openExternal(url));
ipcMain.handle('open-audio-midi', () => {
  shell.openPath('/System/Applications/Utilities/Audio MIDI Setup.app');
});

ipcMain.on('start-interview', (_e, config) => {
  // config: { candidateId, serverUrl, sysDeviceId, micDeviceId }
  store.set('candidateId', config.candidateId);
  store.set('serverUrl', config.serverUrl);
  store.set('sysDeviceId', config.sysDeviceId);
  store.set('micDeviceId', config.micDeviceId);

  if (setupWindow) setupWindow.hide();
  if (!overlayWindow) createOverlayWindow();

  // Send config and show overlay — overlay is pre-created so ready-to-show already fired
  const sendConfig = () => {
    overlayWindow.webContents.send('session-config', {
      candidateId: config.candidateId,
      serverUrl: config.serverUrl,
      sysDeviceId: config.sysDeviceId,
      micDeviceId: config.micDeviceId,
    });
    overlayWindow.show();
  };

  // Check if page is loaded
  if (overlayWindow.webContents.isLoading()) {
    overlayWindow.webContents.once('did-finish-load', sendConfig);
  } else {
    sendConfig();
  }
});

ipcMain.on('stop-interview', () => {
  if (overlayWindow) {
    overlayWindow.webContents.send('stop-session');
    overlayWindow.hide();
  }
  if (setupWindow) setupWindow.show();
  else createSetupWindow();
});

// ─── Keyboard Shortcuts (overlay) ───────────────────────────────────────────
// Lifted from reverse-engineer-littlebird/electron/main.js

function registerShortcuts() {
  // Cmd+Shift+H — Hide/Show overlay
  globalShortcut.register('CommandOrControl+Shift+H', () => {
    if (!overlayWindow) return;
    if (overlayWindow.isVisible()) overlayWindow.hide();
    else overlayWindow.show();
  });

  // Cmd+Shift+P — Pause/Resume
  globalShortcut.register('CommandOrControl+Shift+P', () => {
    sendToOverlay('toggle-pause');
  });

  // Cmd+Shift+L — Toggle click-through / interactive
  globalShortcut.register('CommandOrControl+Shift+L', () => {
    if (!overlayWindow) return;
    overlayWindow._clickThrough = !overlayWindow._clickThrough;
    if (overlayWindow._clickThrough) {
      overlayWindow.setIgnoreMouseEvents(true, { forward: true });
    } else {
      overlayWindow.setIgnoreMouseEvents(false);
    }
    sendToOverlay('interactive-changed', !overlayWindow._clickThrough);
  });

  // Cmd+Shift+S — Show setup window (instead of toggle auto-scroll)
  globalShortcut.register('CommandOrControl+Shift+S', () => {
    if (setupWindow) setupWindow.show();
    else createSetupWindow();
  });

  // Cmd+Shift+A — Toggle auto-scroll on overlay
  globalShortcut.register('CommandOrControl+Shift+A', () => {
    sendToOverlay('toggle-autoscroll');
  });

  // Cmd+Shift+1-5 — Speed
  for (let i = 1; i <= 5; i++) {
    globalShortcut.register(`CommandOrControl+Shift+${i}`, () => {
      sendToOverlay('speed-changed', i);
    });
  }

  // Cmd+Shift+- / Cmd+Shift+= — Opacity
  globalShortcut.register('CommandOrControl+Shift+-', () => {
    if (!overlayWindow) return;
    const current = overlayWindow.getOpacity();
    overlayWindow.setOpacity(Math.max(0.1, current - 0.1));
    sendToOverlay('opacity-changed', Math.round(overlayWindow.getOpacity() * 100));
  });
  globalShortcut.register('CommandOrControl+Shift+=', () => {
    if (!overlayWindow) return;
    const current = overlayWindow.getOpacity();
    overlayWindow.setOpacity(Math.min(1.0, current + 0.1));
    sendToOverlay('opacity-changed', Math.round(overlayWindow.getOpacity() * 100));
  });

  // Cmd+Shift+Arrows — Move overlay
  const MOVE_STEP = 20;
  globalShortcut.register('CommandOrControl+Shift+Left', () => {
    if (!overlayWindow) return;
    const [x, y] = overlayWindow.getPosition();
    overlayWindow.setPosition(x - MOVE_STEP, y);
    snapOverlayToScreen();
  });
  globalShortcut.register('CommandOrControl+Shift+Right', () => {
    if (!overlayWindow) return;
    const [x, y] = overlayWindow.getPosition();
    overlayWindow.setPosition(x + MOVE_STEP, y);
    snapOverlayToScreen();
  });
  globalShortcut.register('CommandOrControl+Shift+Up', () => {
    if (!overlayWindow) return;
    const [x, y] = overlayWindow.getPosition();
    overlayWindow.setPosition(x, y - MOVE_STEP);
    snapOverlayToScreen();
  });
  globalShortcut.register('CommandOrControl+Shift+Down', () => {
    if (!overlayWindow) return;
    const [x, y] = overlayWindow.getPosition();
    overlayWindow.setPosition(x, y + MOVE_STEP);
    snapOverlayToScreen();
  });
}

function sendToOverlay(channel, data) {
  if (overlayWindow && !overlayWindow.isDestroyed()) {
    overlayWindow.webContents.send(channel, data);
  }
}

// ─── App Lifecycle ──────────────────────────────────────────────────────────

app.whenReady().then(() => {
  createSetupWindow();
  createOverlayWindow(); // pre-create but don't show
  registerShortcuts();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (!setupWindow && !overlayWindow) createSetupWindow();
  else if (setupWindow) setupWindow.show();
});

app.on('will-quit', () => {
  globalShortcut.unregisterAll();
});
