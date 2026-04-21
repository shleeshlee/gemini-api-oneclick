# 🎀 Gemini API OneClick

一键部署 Gemini API 多账号智能网关 — 自动轮询、分组路由、图片/视频/音乐生成与编辑、Deep Research，一个端口搞定。

[![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE) [![Version](https://img.shields.io/badge/version-3.0.0-green.svg)](https://github.com/shleeshlee/gemini-api-oneclick/releases)

> **👤 作者:** WanWan
> **📦 开源协议:** AGPL-3.0
> **⚠️ 声明:** 本项目完全免费开源，如果你是付费获取的，你被骗了！

---

## 快速开始

## 选择架构

| 架构 | 推荐对象 | 说明 |
|------|----------|------|
| `worker` | 新用户、RN 这类易炸网机器 | 单容器 + slot，资源占用低，默认推荐 |
| `accounts` | 旧用户、需要每账号独立容器 | legacy 兼容模式，仍可用，但已不再推荐新装 |

**一行命令安装：**

```bash
bash <(curl -Ls https://raw.githubusercontent.com/shleeshlee/gemini-api-oneclick/main/scripts/install.sh)
```

**或者手动 clone：**

```bash
git clone https://github.com/shleeshlee/gemini-api-oneclick.git
cd gemini-api-oneclick
bash scripts/install.sh
```

安装向导会引导你选择架构和配置：

1. **架构** — `worker`（推荐）或 `accounts`（legacy）
2. **账号数量** — 几个 Gemini 账号就填几个
3. **API 密钥** — 客户端调用密钥（可自动生成）
4. **代理设置** — 出站代理（可选）
5. **Gateway 端口** — 统一入口，默认 9880

已有环境重新跑 `install.sh` 可选择更新（保留账号配置）或全新安装。

> 迁移指引只推荐 `accounts -> worker`。如果机器已经处于 “systemd enabled，但实际被 `nohup` 手工进程取代” 的混合状态，先清掉野进程并恢复单一托管链路，再执行安装器。

### 迁移与混合态清理

仅推荐 `accounts -> worker` 单向迁移，不建议把 `worker -> accounts` 当成常规回退路径。

如果机器已经处于 “systemd 仍 enabled，但实际是 `nohup python3 gateway.py` 在顶着” 的混合状态，先清场再迁移：

1. 停掉手工 `nohup` 的 gateway 进程
2. 确认 `systemctl status gemini-gateway` 与实际监听端口一致
3. 确认只保留当前架构对应的 compose / systemd 链路
4. 检查 `/gateway/status` 的 `mode` 与 `.env` 的 `WORKER_MODE` 一致
5. 再执行 `bash scripts/install.sh` 做升级或迁移

## 功能一览

### 核心能力

- ⚡ **智能网关** — 自动轮询所有账号，故障节点静默跳过，最多 5 次重试
- 🏷️ **分组路由** — 按模型前缀自动分流到不同账号池
- 🔍 **健康检查** — 后台每 30 秒检测真实状态，错误账号自动冷却恢复
- ⏱️ **超时容错** — 三层超时保护 + 冷却机制，防止请求积压
- 🔒 **安全加固** — 速率限制、timing-safe 认证、防暴力破解
- ➕ **弹性扩容** — 随时通过 `manage.sh` 新增/删除账号
- 🧊 **双部署模式** — 单容器（1 进程管所有账号，省内存）或多容器（每账号独立端口）

### 创作工作室

- 🎨 **图片生成** — OpenAI 兼容端点，30+ 内置风格模板
- 🎬 **视频生成** — Veo 视频生成，支持图片/视频作为输入素材
- 🎵 **音乐生成** — Lyria 音乐生成，返回 MP3 + MP4 音乐视频
- 🔬 **Deep Research** — 深度研究，异步任务模式，自动提取来源引用
- ✏️ **多模态编辑** — 上传图片/视频/音频，转换为任意格式（图→视频、视频→配乐等）
- 🔍 **风格解析** — AI 分析素材的视觉/音频风格，保存为可复用模板
- ✨ **提示词优化** — 按目标（图片/视频/音乐）分别优化提示词
- 📋 **并行任务** — 多任务同时生成，每个任务独立 tab，实时显示容器调度过程
- 🖼️ **项目图库** — 保存、管理、拖拽编辑生成的图片
- 🎥 **项目视频库** — 保存、播放、下载生成的视频

### 管理面板

- 📊 **账号状态** — 编号、名称、分组、健康状态一目了然
- 🍪 **Cookie 管理** — 面板内直接部署 Cookie，单个账号重启/测试/禁用/删除
- 📋 **运行日志** — 面板内查看日志（自动过滤健康检查噪音）
- 🏷️ **分组管理** — 创建分组、批量分配账号、重命名、删除

## 架构

支持两种部署模式，Gateway 层完全一致：

**worker 架构（推荐）**

```
    用户请求 ──> Gateway :9880 ──> Worker :7860
                 分组路由            ┌─ Slot 1 (账号 1)
                 智能轮询            ├─ Slot 2 (账号 2)
                 管理面板            └─ Slot N (账号 N)
                                         │
                                    Gemini Web API
```

一个 Worker 进程管理所有账号，内存占用低（32 账号约 140MB），适合大多数场景。

**accounts 架构（legacy）**

```
    用户请求 ──> Gateway :9880 ──┬─ Container 1 :8001
                 分组路由         ├─ Container 2 :8002
                 智能轮询         └─ Container N :800N
                 管理面板              │
                                 Gemini Web API
```

每个账号独立 Docker 容器和端口，进程级隔离，但维护成本更高。

- **Gateway** — 统一接收请求，按分组路由 + 轮询分发，管理账号状态
- **Worker / Container** — 持有 Gemini Cookie，报告真实状态
- **不需要外部负载均衡** — Gateway 自带轮询、故障转移和分组路由

## API 端点

请求头带 `Authorization: Bearer 你的API密钥`。

| 端点 | 说明 |
|------|------|
| `POST /v1/chat/completions` | 聊天（OpenAI 兼容，支持流式） |
| `POST /v1/images/generations` | 图片生成/编辑（支持风格/质量/素材上传） |
| `POST /v1/videos/generations` | 视频生成（支持文本/图片/视频输入） |
| `POST /v1/music/generations` | 音乐生成（Lyria，返回 MP3 + MP4） |
| `POST /v1/tasks/create` | 创建并行生成任务，立即返回 task_id |
| `GET /v1/tasks/{id}/stream` | SSE 流式获取任务状态和结果 |
| `POST /v1/research` | 启动 Deep Research，返回 task_id |
| `GET /v1/research/{id}` | 查询研究进度和结果 |
| `GET /v1/models` | 可用模型列表 |
| `GET /` | 管理面板 + 创作工作室 |

兼容 OpenAI 格式，可直接接入 SillyTavern、NextChat、NewAPI、Cherry Studio 等。

### 视频生成

```bash
curl -X POST http://你的IP:9880/v1/videos/generations \
  -H "Authorization: Bearer 你的API密钥" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "a kitten playing with yarn", "model": "gemini-3-flash"}'
```

视频生成需要 1-5 分钟（Veo），API 自动轮询等待完成，返回 base64 数据和下载 URL。

## 超时与容错

| 层级 | 生图 | 生视频 | 生音乐 | 生文 | Deep Research |
|------|------|--------|--------|------|---------------|
| 容器内部 | 300s | 300s | 300s | 300s | 异步任务 |
| Gateway | 180s | 330s | 120s | 300s | 60s（返回 task_id） |
| 客户端建议 | ≥120s | ≥360s | ≥120s |

## 常用命令

| 命令 | 说明 |
|------|------|
| `bash scripts/install.sh` | 交互式安装/更新 |
| `bash scripts/manage.sh` | 当前架构管理（添加/删除/状态/重建/卸载） |
| `bash scripts/uninstall.sh` | 按当前架构精确卸载，保留 `envs/` 和 `state/` |

> `accounts` 架构下，用 `scripts/safe-deploy.sh` 分批重启，禁止 `docker compose restart` 全量操作。

## Discord Bot

配套的 Discord Bot —— [Gemini 体验站 (gem-bot)](https://github.com/shleeshlee/gem-bot)，接入 Gateway 即可让 Discord 用户直接生图和写作。

## 致谢

底层 Gemini Web API 通信基于以下项目，已深度定制（新增视频/图片编辑、raw 响应追踪、Cookie 管理等）：

| 项目 | 作者 | 用途 |
|------|------|------|
| [Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) | HanaokaYuzu | Gemini Web API 通信库原版 |
| [xob0t/Gemini-API](https://github.com/xob0t/Gemini-API) | xob0t | curl_cffi 适配分支，本项目的 fork 基础 |
| [Gemini-FastAPI](https://github.com/Nativu5/Gemini-FastAPI) | Nativu5 | 早期架构启发 |

Gateway、分组路由、管理面板、创作工作室、部署工具链等为本项目独立开发。

Thanks to everyone on LinuxDo for their support! 欢迎大家加入 https://linux.do/ 进行自由的技术交流。

## 许可证

[AGPL-3.0](LICENSE)

---

**🎀 Gemini API OneClick v3.0.0** by WanWan | [GitHub](https://github.com/shleeshlee/gemini-api-oneclick)
