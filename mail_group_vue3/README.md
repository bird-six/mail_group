# 邮件群发助手前端

Vue 3 + TypeScript 界面，由 Electron 桌面窗口加载本地 FastAPI 服务提供的构建产物。
包括首页任务、邮件内容与随机模板、目标邮箱、邮箱集群、数据面板、邮件汇总及使用说明。

普通用户请查看 [项目说明](../README.md) 和 [桌面版使用说明](../release/桌面版使用说明.txt)，使用打包后的 Windows 程序。
应用内“使用说明”包含操作目录、当前按钮名称、IMAP 设置和任务恢复步骤。

## 开发与校验

在此目录安装锁定的前端依赖并构建：

```powershell
npm ci
npm run build
```

构建同时执行类型检查，输出到 `../backend/dist`，后端和桌面程序会直接使用这些文件。
完整桌面启动和打包命令见 [项目根目录文档](../README.md#开发与打包)。
单独执行 `npm run dev` 只启动前端开发服务器，完整功能仍需要已启动并正确配置会话的后端。

```powershell
npm exec -- vitest run
npm exec -- vitest run --config audit.config.ts
npm exec -- eslint src audit.config.ts integration.config.ts
```

Vue/FastAPI 集成校验在项目根目录运行：

```powershell
.venv/Scripts/python.exe -B diagnostics/run_audit_integration.py
```

测试使用合成邮箱和临时数据库，不应读取或连接真实邮箱。
不要将授权码、个人邮箱、邮件正文、数据目录、诊断截图或日志写入示例或提交到仓库。

## 更新使用说明

界面内帮助位于 `src/App.vue` 的使用说明区域；同时更新根目录 README 和 `release/桌面版使用说明.txt`。
修改界面帮助后重新构建前端。面向桌面用户交付时，还需按根目录文档重新打包，让程序内说明与离线手册一致。
