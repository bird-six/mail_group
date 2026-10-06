import { mergeConfig } from 'vite'
import { defineConfig } from 'vitest/config'
import viteConfig from './vite.config'

export default mergeConfig(viteConfig, defineConfig({
  test: { include: ['src/__tests__/*.integration.ts'], environment: 'jsdom' },
}))
