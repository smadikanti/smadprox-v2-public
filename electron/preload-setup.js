const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('smadprox', {
  // Store
  storeGet: (key) => ipcRenderer.invoke('store-get', key),
  storeSet: (key, val) => ipcRenderer.invoke('store-set', key, val),
  storeAll: () => ipcRenderer.invoke('store-all'),

  // External links
  openExternal: (url) => ipcRenderer.invoke('open-external', url),

  // Interview lifecycle
  startInterview: (config) => ipcRenderer.send('start-interview', config),
  stopInterview: () => ipcRenderer.send('stop-interview'),
});
