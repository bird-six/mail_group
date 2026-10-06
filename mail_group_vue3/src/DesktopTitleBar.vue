<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
const desktop = window.desktop
const maximized = ref(false)
let unsubscribe: (() => void) | undefined
onMounted(async () => {
  if (!desktop) return
  maximized.value = (await desktop.getWindowState()).maximized
  unsubscribe = desktop.onWindowState(state => { maximized.value = state.maximized })
})
onUnmounted(() => unsubscribe?.())
</script>

<template>
  <header v-if="desktop" class="desktop-titlebar">
    <div class="desktop-brand"><svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="3"/><path d="m4 7 8 6 8-6"/></svg><span>邮件群发助手</span><span class="desktop-edition">桌面版</span></div>
    <div class="desktop-window-actions">
      <button class="data-folder" aria-label="打开数据文件夹" title="打开数据文件夹" @click="desktop.openDataFolder()"><svg viewBox="0 0 16 16"><path d="M2 4h5l2 2h5v7H2z"/></svg></button>
      <button aria-label="最小化" title="最小化" @click="desktop.minimize()"><svg viewBox="0 0 16 16"><path d="M3 8h10"/></svg></button>
      <button :aria-label="maximized ? '还原窗口' : '最大化'" :title="maximized ? '还原窗口' : '最大化'" @click="desktop.toggleMaximize()"><svg viewBox="0 0 16 16"><path v-if="maximized" d="M5 3h8v8M3 5h8v8H3z"/><rect v-else x="3.5" y="3.5" width="9" height="9"/></svg></button>
      <button class="close-window" aria-label="关闭窗口" title="关闭窗口" @click="desktop.close()"><svg viewBox="0 0 16 16"><path d="m4 4 8 8M12 4l-8 8"/></svg></button>
    </div>
  </header>
</template>

<style scoped>
.desktop-titlebar{position:fixed;inset:0 0 auto;height:42px;display:flex;align-items:center;justify-content:space-between;background:#172230;color:#eef3f9;z-index:10000;-webkit-app-region:drag;user-select:none;border-bottom:1px solid #293647;font:12px "Segoe UI","Microsoft YaHei",sans-serif}
.desktop-brand{display:flex;align-items:center;gap:10px;padding-left:20px;font-weight:600;letter-spacing:.04em}.desktop-brand>svg{width:20px;height:20px;stroke:#9bbadb;fill:none;stroke-width:1.6}.desktop-edition{font-size:10px;font-weight:400;color:#a5b4c7;letter-spacing:0;padding-left:4px}
.desktop-window-actions{height:100%;display:flex;-webkit-app-region:no-drag}.desktop-window-actions button{height:100%;width:46px;padding:0;border:0;border-radius:0;background:transparent;color:inherit;display:grid;place-items:center;cursor:pointer}.desktop-window-actions button:hover{background:#304155}.desktop-window-actions button:focus-visible{outline:2px solid #a7cefa;outline-offset:-3px}.desktop-window-actions .close-window:hover{background:#c93642}.desktop-window-actions svg{width:16px;height:16px;fill:none;stroke:currentColor;stroke-width:1.1}.desktop-window-actions .data-folder{margin-right:10px;color:#b8c7d8}
</style>
