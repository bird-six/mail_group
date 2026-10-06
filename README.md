# 邮件群发助手 · Electron 桌面版

Windows 10 / 11 x64 桌面应用，提供邮箱管理、批量收件人导入、邮件模板及附件、发送任务与统计。
窗口采用自定义可拖动标题区域，无系统原生顶部边框；支持最小化、最大化和关闭。

## 直接使用

打开 `release/desktop-v3`，选择其中一种：

- `MailGroup-3.0.0-x64-portable.exe`：双击启动，免安装。
- `MailGroup-3.0.0-x64-nsis.exe`：运行安装向导，之后从桌面快捷方式启动。

两个版本均包含 Electron、Python 和后端依赖，使用者不需要开发环境。
按“邮箱集群 → 目标邮箱 → 邮件内容 → 首页开始群发”完成配置。
实际投递需要网络、可使用 SMTP 的邮箱与授权码。
参见 [详细使用说明](release/桌面版使用说明.txt)。

数据默认位于 `%APPDATA%\MailGroup\data`，可点击窗口顶部文件夹按钮打开。
关闭应用后备份整个 data 目录；新安装包不含个人数据。旧版迁移步骤见使用说明。
Git 仓库仅维护源码、构建脚本和合成测试；安装包与运行数据保留在本地。旧版运行目录不再提交。

## 本次修复

- 本地 API 使用随机会话认证，阻止其他网站跨域调用；SMTP 授权码不再返回页面。
- 提交时校验数据库中收件人和发件人的最新状态；重复提交保持幂等。
- 发送任务、分配、附件、日志持久保存；重启可继续未发送项，未知结果不会自动重发。
- 同一邮箱跨任务统一限速；成功后禁用收件人，页面定时同步。
- 模板附件完整加载后才能发送；上传统一校验，中文与特殊字符附件可下载。
- 修复批量导入覆盖列表、重复地址编辑错误、历史任务详情和跨年统计。
- 支持真正的 HTML 邮件；修复启动脚本、依赖路径和小窗口导航。

## 开发与打包

开发/构建机器需要 Windows x64、Python 3.12+、Node.js 24+。在项目根目录运行：

```powershell
python release/build_desktop.py --verify
```

脚本创建 `.venv`、安装锁定依赖、校验官方 Electron 下载的 SHA256，构建前端、冻结 Python 后端，
生成安装版、免安装版、说明文件和 `SHA256SUMS.txt`。已准备好依赖时可加 `--skip-install`。
PyInstaller 仅打包代码、静态界面和运行库，不复制 `backend/data`。
此版本未配置商业代码签名证书，Windows 可能显示未知发布者提示。

本地开发桌面窗口：先完成依赖安装和前端构建，然后运行：

```powershell
cd desktop
npm start
```

单独使用后端/浏览器模式：

```powershell
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r backend/requirements.txt
.venv/Scripts/python.exe -B backend/main.py
```

服务绑定回环地址的随机空闲端口，自动打开带临时会话的浏览器页面。
桌面模式由 Electron 启停后端，不需另开终端。
开发时若使用 Vite 独立端口，可显式设置 `MAIL_GROUP_DEV_ORIGIN`；正式桌面版不允许开发跨域配置。

## 验证

```powershell
.venv/Scripts/python.exe -B -m pytest backend -q -p no:cacheprovider
.venv/Scripts/python.exe -B diagnostics/run_audit_integration.py
cd mail_group_vue3
npm exec -- vitest run
npm exec -- vitest run --config audit.config.ts
npm exec -- eslint src audit.config.ts
npm run build
cd ../desktop
node_modules/electron/dist/electron.exe smoke.cjs
```

测试使用临时数据库和模拟 SMTP，不投递真实邮件；不验证服务商配额或收件箱最终送达。
集成测试使用自动生成的 example.com 地址，不依赖私人收件人清单。
本地验证结果和截图位于 `diagnostics/desktop-v3`，不会提交到仓库。

## 项目结构

| 目录 | 用途 |
| --- | --- |
| `backend` | FastAPI、SQLite、SMTP 调度和测试 |
| `mail_group_vue3` | Vue 3 + TypeScript 页面 |
| `desktop` | Electron 主进程、受限 IPC、自定义窗口与原生测试 |
| `release/build_desktop.py` | 可复现的构建入口 |
| `release/desktop-v3` | 本次桌面交付物 |

本项目仅供学习参考使用。
