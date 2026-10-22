const { contextBridge, ipcRenderer } = require('electron');
contextBridge.exposeInMainWorld('workbenchSettings', {
  read: () => ipcRenderer.invoke('settings:read'),
  save: data => ipcRenderer.invoke('settings:save', data),
  openFolder: () => ipcRenderer.invoke('settings:folder')
});
