# 🎀 Gemini API OneClick

一键部署 Gemini API 多账号智能网关 — 自动轮询、分组路由、图片/视频生成与编辑，一个端口搞定。

[![License](https://img.shields.io/badge/license-AGPL--3.0-blue.svg)](LICENSE) [![Version](https://img.shields.io/badge/version-2.0.0-green.svg)](https://github.com/shleeshlee/gemini-api-oneclick/releases)

> **👤 作者:** WanWan
> **📦 开源协议:** AGPL-3.0
> **⚠️ 声明:** 本项目完全免费开源，如果你是付费获取的，你被骗了！

---

## 快速开始

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

安装向导 5 步完成：

1. **容器数量** — 几个 Gemini 账号就填几个
2. **端口范围** — 容器起始端口（自动检测冲突）
3. **API 密钥** — 客户端调用密钥（可自动生成）
4. **代理设置** — 出站代理（可选）
5. **Gateway 端口** — 统一入口，默认 9880

已有环境重新跑 `install.sh` 可选择更新（保留账号配置）或全新安装。

## 功能一览

### 核心能力

- ⚡ **智能网关** — 自动轮询所有容器，故障节点静默跳过，最多 5 次重试
- 🏷️ **分组路由** — 按模型前缀自动分流到不同容器池
- 🔍 **健康检查** — 后台每 30 秒检测容器真实状态，错误容器自动冷却恢复
- ⏱️ **超时容错** — 三层超时保护 + 容器冷却机制，防止请求积压
- 🔒 **安全加固** — 速率限制、timing-safe 认证、防暴力破解
- ➕ **弹性扩容** — 随时通过 `manage.sh` 新增/删除容器
- 🔄 **安全部署** — `safe-deploy.sh` 分批重启，防止触发机房 DDoS 防护

### 创作工作室

- 🎨 **图片生成** — OpenAI 兼容端点，30+ 内置风格模板
- 🎬 **视频生成** — Veo 视频生成，支持图片/视频作为输入素材
- ✏️ **素材编辑** — 上传图片或视频，用自然语言描述修改内容
- 🔍 **风格解析** — AI 分析图片/视频的视觉风格，保存为可复用模板
- ✨ **提示词优化** — AI 自动优化生成提示词
- 🖼️ **项目图库** — 保存、管理、拖拽编辑生成的图片
- 🎥 **项目视频库** — 保存、播放、下载生成的视频

### 管理面板

- 📊 **容器状态** — 编号、端口、名称、分组、健康状态一目了然
- 🍪 **Cookie 管理** — 面板内直接部署 Cookie，单个容器重启/测试/禁用
- 📋 **容器日志** — 面板内查看 Docker 日志（自动过滤健康检查噪音）
- 🏷️ **分组管理** — 创建分组、批量分配容器、重命名、删除

## 架构

```
                    ┌──────────────────────────┐
    用户请求 ──────>│    Gateway :9880 (总入口)  │
                    │  分组路由 + 智能轮询       │
                    │  健康检查 + 管理面板       │
                    └──────┬───────────────────┘
                           │ 按分组/轮询分发
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         Container 1  Container 2  Container N
         (账号 1)     (账号 2)     (账号 N)
              │            │            │
              ▼            ▼            ▼
           Gemini Web API (Cookie 认证)
```

- **Gateway（管理者）** — 宿主机进程，统一接收请求，按分组路由 + 轮询分发，管理容器状态（冷却/禁用/重启）
- **Container（传感器）** — 每个容器一个 FastAPI 实例，使用独立的 Gemini Cookie，只报告真实状态
- **不需要外部负载均衡** — Gateway 自带轮询、故障转移和分组路由

## API 端点

请求头带 `Authorization: Bearer 你的API密钥`。

| 端点 | 说明 |
|------|------|
| `POST /v1/chat/completions` | 聊天（OpenAI 兼容，支持流式） |
| `POST /v1/images/generations` | 图片生成/编辑（支持风格/质量/素材上传） |
| `POST /v1/videos/generations` | 视频生成（支持文本/图片/视频输入） |
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

| 层级 | 生图 | 生视频 | 生文 |
|------|------|--------|------|
| 容器内部 | 300s | 300s | 300s |
| Gateway | 180s | 330s | 300s |
| 客户端建议 | ≥120s | ≥360s | ≥120s |

## 常用命令

| 命令 | 说明 |
|------|------|
| `make install` | 交互式安装 |
| `make manage` | 容器管理 |
| `make up` | 构建并分批启动 |
| `bash scripts/safe-deploy.sh --build` | 重建镜像 + 分批部署 |

> ⚠️ **禁止全量重启**（`docker compose restart`），务必用 `safe-deploy.sh` 分批操作。

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

## 许可证

[AGPL-3.0](LICENSE)

---

**🎀 Gemini API OneClick v2.0.0** by WanWan | [GitHub](https://github.com/shleeshlee/gemini-api-oneclick)
