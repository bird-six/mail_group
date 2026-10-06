const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('desktop', {
  minimize: () => ipcRenderer.invoke('window:minimize'),
  toggleMaximize: () => ipcRenderer.invoke('window:maximize'),
  close: () => ipcRenderer.invoke('window:close'),
  openDataFolder: () => ipcRenderer.invoke('window:data-folder'),
  getWindowState: () => ipcRenderer.invoke('window:state'),
  onWindowState: callback => {
    const listener = (_event, value) => callback({ maximized: Boolean(value.maximized) })
    ipcRenderer.on('window:state', listener)
    return () => ipcRenderer.removeListener('window:state', listener)
  },
})
