# 邮件群发助手 · Electron 桌面版

Windows 10 / 11 x64 桌面应用，提供邮箱管理、批量收件人导入、多模板随机发送、统一收发邮件汇总、发送任务与统计。
窗口采用自定义可拖动标题区域，无系统原生顶部边框；支持最小化、最大化和关闭。

## 直接使用

打开 `release/desktop-v3`，选择其中一种：

- `MailGroup-3.2.0-x64-portable.exe`：双击启动，免安装。
- `MailGroup-3.2.0-x64-nsis.exe`：运行安装向导，之后从桌面快捷方式启动。

两个版本均包含 Electron、Python 和后端依赖，使用者不需要开发环境。
按“邮箱集群 → 目标邮箱 → 邮件内容 → 首页开始群发”完成配置。
实际投递需要网络、可使用 SMTP 的邮箱与授权码。
参见 [详细使用说明](release/桌面版使用说明.txt)。

数据默认位于 `%APPDATA%\MailGroup\data`，可点击窗口顶部文件夹按钮打开。
关闭应用后备份整个 data 目录；新安装包不含个人数据。旧版迁移步骤见使用说明。
Git 仓库仅维护源码、构建脚本和合成测试；安装包与运行数据保留在本地。旧版运行目录不再提交。

## 多模板随机发送

在“邮件内容”中勾选多个模板，或使用“全选”，即可切换为“随机使用选中模板”。
每位收件人独立、等概率地抽取一个模板，允许重复抽到同一个；只选一个模板时全部使用该模板。
每个模板的主题、纯文本/HTML 正文和附件一起发送，不混入编辑器中的内容或附件。
任务创建时会固定抽取结果并保存副本，暂停恢复不会重新抽取；删除原模板也不影响已创建的任务。
任务详情显示各收件人的邮件主题。清空选择会阻止随机发送；切换“当前编辑内容”可使用原来的发送方式。
从 3.0 升级时会自动保存 before-v3.1.sqlite3 数据库备份，请继续使用新版程序。

## 统一收发邮件汇总

进入“邮件汇总”，点击“同步全部邮箱”，即可集中查看各账号的收件箱、已发送邮件及本应用成功发送的记录。
可以按账号、收发类型筛选，搜索主题和往来地址，分页查看，点击邮件读取正文。
本机发送记录可通过“刷新列表”查看，不依赖 IMAP；升级时会导入具有完整内容的历史成功任务。
本应用新发送的邮件带独立 Message-ID，可与原邮箱中的同封已发送副本合并；旧版记录可能同时显示两个来源。

- 先在服务商设置中开启 **IMAP**。与 SMTP 共用授权码；常见 SMTP 地址可自动对应 IMAP 地址，自定义账号需在“邮箱集群 → 编辑 → IMAP 设置”中填写。
- 默认使用 SSL/TLS 993，也支持 STARTTLS；证书必须有效，不允许明文登录。当前不支持仅允许 OAuth 的账号。
- 每次同步为每个账号的收件箱、已发送文件夹各补充最多 100 封，优先获取新邮件；继续点击同步可补全历史。单账号失败不影响其他账号，账号关闭群发后仍可同步。
- 自动识别已发送文件夹；若失败，可填写服务商中的文件夹名称。服务商未存储的发送副本不会凭空出现在 IMAP 中，本应用成功发送的记录仍保留。
- 同步仅在本机缓存，查看使用只读 IMAP，不修改原邮箱已读状态。已查看的正文可离线阅读，HTML 转为纯文本，不加载远程图片或运行脚本。
- 附件展示名称，需到原邮箱打开。正文最多读取 10 MiB 原始邮件、展示 50 万字符，超限时显示截断提示。
- 更换邮箱地址或删除账号会移除对应汇总缓存；修改 IMAP 配置或授权码会清理同步缓存，保留本机发送记录。升级 3.1 数据库时自动生成 `before-v3.2.sqlite3` 备份。

协议实现参考 [Python imaplib](https://docs.python.org/3.12/library/imaplib.html)、[IMAP 规范](https://www.rfc-editor.org/rfc/rfc9051.html)和[已发送文件夹标记规范](https://www.rfc-editor.org/rfc/rfc6154.html)。

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

测试使用临时数据库、模拟 SMTP / IMAP 和本机回环 IMAP 协议服务器，不连接真实邮箱或投递邮件；不验证服务商配额、账号权限或最终送达。
集成测试使用自动生成的 example.com 地址，不依赖私人收件人清单。
本地验证结果和截图位于 `diagnostics/desktop-v3`，不会提交到仓库。

## 项目结构

| 目录 | 用途 |
| --- | --- |
| `backend` | FastAPI、SQLite、SMTP 调度、IMAP 汇总和测试 |
| `mail_group_vue3` | Vue 3 + TypeScript 页面 |
| `desktop` | Electron 主进程、受限 IPC、自定义窗口与原生测试 |
| `release/build_desktop.py` | 可复现的构建入口 |
| `release/desktop-v3` | 本次桌面交付物 |

本项目仅供学习参考使用。
