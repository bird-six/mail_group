/// <reference types="vite/client" />

interface Window {
  desktop?: {
    minimize(): Promise<void>
    toggleMaximize(): Promise<void>
    close(): Promise<void>
    openDataFolder(): Promise<void>
    getWindowState(): Promise<{ maximized: boolean }>
    onWindowState(callback: (state: { maximized: boolean }) => void): () => void
  }
}
