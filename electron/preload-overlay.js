const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('noscreen', {
  onSpeedChanged: (cb) => ipcRenderer.on('speed-changed', (_e, v) => cb(v)),
  onToggleAutoscroll: (cb) => ipcRenderer.on('toggle-autoscroll', () => cb()),
  onTogglePause: (cb) => ipcRenderer.on('toggle-pause', () => cb()),
  onOpacityChanged: (cb) => ipcRenderer.on('opacity-changed', (_e, v) => cb(v)),
  onInteractiveChanged: (cb) => ipcRenderer.on('interactive-changed', (_e, v) => cb(v)),
  onSessionConfig: (cb) => ipcRenderer.on('session-config', (_e, v) => cb(v)),
  onStopSession: (cb) => ipcRenderer.on('stop-session', () => cb()),
});
