# 邮件群发系统

一个基于 FastAPI 构建的邮件群发系统，支持多发送邮箱、批量收件人管理、附件发送、任务调度与统计监控。

## 功能特性

- **发送邮箱管理** — 支持配置多个 SMTP 发送邮箱（QQ 邮箱等），可单独启用/禁用
- **收件人管理** — 批量导入收件人，支持单个/批量启用或禁用
- **邮件内容保存** — 支持保存邮件模板，重复使用
- **附件发送** — 支持上传附件（单文件最大 50MB），支持多个附件
- **智能分配** — 多个发送邮箱时自动轮询分配给收件人
- **异步发送** — 基于 asyncio 实现异步并发发送，同一发送邮箱的邮件间隔 20~40 秒
- **发送统计** — 按小时/天/周/月统计发送成功率，查看发送方和收件方的详细报表
- **失败重试** — 自动记录发送失败的收件人，支持查看失败原因分析
- **前端界面** — 提供完整的 Web 管理界面（基于 Vite 构建）

## 技术栈

| 层次 | 技术 |
|------|------|
| 后端框架 | FastAPI + Uvicorn |
| 数据库 | SQLite3（WAL 模式） |
| 邮件协议 | SMTP over SSL (465) |
| 前端 | Vite + Vue（预构建静态文件） |
| 异步 | asyncio + threading |
| 验证 | Pydantic v2 |

## 快速开始

### 环境要求

- Python 3.12+
- 虚拟环境（推荐）

### 安装与运行

```bash
# 1. 进入项目目录
cd backend

# 2. 创建并激活虚拟环境（如尚未创建）
python -m venv .venv
.venv\Scripts\activate  # Windows

# 3. 安装依赖
pip install -r requirements.txt

# 4. 启动服务
python main.py
```

服务启动后会自动打开浏览器访问 `http://127.0.0.1:8000`。

### 使用启动脚本

在 `release` 目录下直接双击 `启动系统.bat` 即可一键启动（会自动打开浏览器）。

## 项目结构

```
mail_group/
├── backend/                     # 后端代码
│   ├── main.py                  # FastAPI 应用主入口
│   ├── test_main.py             # 测试用例
│   ├── data/                    # 运行时数据目录
│   │   ├── mail_group.sqlite3   # SQLite 数据库
│   │   └── saved_attachments/   # 保存的附件文件
│   └── dist/                    # 预构建的前端静态文件
│       ├── index.html
│       ├── favicon.ico
│       └── assets/
├── release/                     # 发布版本
│   ├── 邮件群发系统_v2/          # v2 发布包
│   ├── 启动系统.bat              # 一键启动脚本
│   └── mail.svg                 # 应用图标
└── requirements.txt             # Python 依赖清单
```

## API 接口

### 系统

| 方法   | 路径          | 说明     |
|--------|---------------|----------|
| GET    | `/api/health` | 健康检查 |

### 发送邮箱

| 方法   | 路径                                 | 说明             |
|--------|--------------------------------------|------------------|
| GET    | `/api/sender-configs`                | 获取发送邮箱列表 |
| POST   | `/api/sender-configs`                | 新增发送邮箱     |
| PUT    | `/api/sender-configs/{id}`           | 更新发送邮箱     |
| DELETE | `/api/sender-configs/{id}`           | 删除发送邮箱     |
| PATCH  | `/api/sender-configs/{id}/toggle`    | 切换启用/禁用    |

### 收件人

| 方法   | 路径                             | 说明             |
|--------|----------------------------------|------------------|
| GET    | `/api/recipients`                | 获取收件人列表   |
| POST   | `/api/recipients`                | 新增收件人       |
| POST   | `/api/recipients/batch`          | 批量新增收件人   |
| DELETE | `/api/recipients/{id}`           | 删除收件人       |
| PATCH  | `/api/recipients/{id}/toggle`    | 切换启用/禁用    |
| PATCH  | `/api/recipients/{id}`           | 更新收件人信息   |
| POST   | `/api/recipients/batch-toggle`   | 批量启用/禁用    |

### 邮件任务

| 方法   | 路径                        | 说明                 |
|--------|-----------------------------|----------------------|
| POST   | `/api/tasks`                | 创建并执行发送任务   |
| GET    | `/api/tasks`                | 获取任务列表         |
| GET    | `/api/tasks/{task_id}`      | 获取任务详情         |

### 保存的邮件

| 方法   | 路径                                          | 说明               |
|--------|-----------------------------------------------|--------------------|
| POST   | `/api/saved-mails`                            | 保存邮件模板       |
| GET    | `/api/saved-mails`                            | 获取邮件模板列表   |
| GET    | `/api/saved-mails/{id}`                       | 获取邮件模板详情   |
| DELETE | `/api/saved-mails/{id}`                       | 删除邮件模板       |
| GET    | `/api/saved-mails/{id}/attachments/{file}`   | 下载附件           |

### 统计

| 方法   | 路径                                        | 说明                 |
|--------|---------------------------------------------|----------------------|
| GET    | `/api/stats?period=hour`                    | 发送统计（支持 hour/day/week/month） |
| GET    | `/api/stats/sender-breakdown`               | 各发送邮箱统计       |
| GET    | `/api/stats/recipient-breakdown`            | 各收件人统计         |
| GET    | `/api/stats/failure-reasons`                | 失败原因分析         |
| GET    | `/api/stats/failed-recipients`              | 发送失败收件人列表   |
| GET    | `/api/stats/failed-recipients/total`        | 失败收件人总数       |
| GET    | `/api/stats/failed-recipients/emails`       | 失败收件人邮箱列表   |

## 配置说明

### 发送邮箱（QQ 邮箱为例）

1. 登录 QQ 邮箱，进入 **设置 → 账户**
2. 开启 **POP3/SMTP 服务**
3. 生成 **授权码**（SMTP 登录密码）
4. 在系统中添加发送邮箱：
   - 邮箱地址：`your_email@qq.com`
   - 授权码：上述步骤生成的授权码
   - SMTP 服务器：`smtp.qq.com`
   - SMTP 端口：`465`

### 附件限制

- 单文件最大：50MB
- 禁止类型：`.exe`, `.bat`, `.cmd`, `.com`, `.scr`, `.js`, `.vbs`, `.msi`, `.jar`

## 运行测试

```bash
cd backend
pytest test_main.py -v
```

## 许可

本项目仅供学习参考使用。
