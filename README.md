# 🎀 Gemini API OneClick v0.1.0

一键部署 Gemini API 多账号网关，自带 Cookie 管理面板和渠道熔断守卫。

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.1.0-green.svg)]()

> **👤 作者:** WanWan
> **📦 开源协议:** MIT (免费使用，保留署名)
> **⚠️ 声明:** 本项目完全免费开源，如果你是付费获取的，你被骗了！

---

## 快速开始

```bash
git clone https://github.com/shleeshlee/gemini-api-oneclick.git
cd gemini-api-oneclick
bash scripts/install.sh
```

交互式安装向导会引导你完成：

1. 容器数量（几个账号）
2. API 密钥配置
3. 代理设置
4. Cookie 管理面板
5. 渠道熔断守卫（可选，接入 NewAPI）

## 功能一览

- 🚀 **交互式安装** — AccBox 风格向导，问几个问题就装好
- 🍪 **Cookie 管理面板** — Web UI 管理每个账号的 Cookie，填完一键重启单个容器
- 🛡️ **渠道熔断守卫** — 容器报错超 3 次自动禁用渠道，重启后自动恢复（NewAPI 面板可见）
- ➕ **弹性扩容** — 随时通过 `manage.sh` 新增容器
- 🔄 **一键更新** — 已安装的环境重新跑 `install.sh` 即可更新

## 容器管理

```bash
bash scripts/manage.sh
# 或
make manage
```

菜单选项：
- **[1] 新增容器** — 指定数量，自动创建 env、生成 compose、增量启动
- **[2] 查看状态** — 容器运行状态 + 健康检查
- **[3] 完整卸载** — 清理容器和配置（保留 envs/ 防误删）

## Cookie 管理面板

安装完成后访问 `http://你的IP:9880`（默认端口）。

功能：
- 查看所有账号及容器状态
- 编辑单个账号的 Cookie
- 一键保存 + 重启该容器
- 本地备注（浏览器 localStorage 存储）
- 导出账号摘要

## 架构

```
Port 8001  ──> Container 1 (account1.env) ──> Gemini API
Port 8002  ──> Container 2 (account2.env) ──> Gemini API
...
Port 800N  ──> Container N (accountN.env) ──> Gemini API

Port 9880  ──> Cookie Manager (宿主机, systemd)
```

每个容器跑一个独立的 FastAPI 实例，使用各自的 Gemini Cookie。你的负载均衡器（如 NewAPI）在多个端口间分发请求。

## 常用命令

| 命令 | 说明 |
|------|------|
| `make install` | 运行交互式安装 |
| `make generate` | 重新生成 docker-compose |
| `make up` | 启动所有容器 |
| `make down` | 停止所有容器 |
| `make restart` | 重启所有容器 |
| `make status` | 查看容器状态 |
| `make logs` | 实时查看日志 |
| `make health` | 运行健康检查 |
| `make manage` | 容器管理菜单 |
| `make guard-run` | 手动执行一次熔断检查 |
| `make guard-install` | 安装熔断定时任务 |
| `make guard-remove` | 移除熔断定时任务 |

## 渠道熔断守卫（可选）

接入 NewAPI 面板，自动管理渠道状态：

1. 在 `.env` 中设置 `ENABLE_CHANNEL_GUARD=true`
2. 配置 `NEWAPI_DB_PASS`（有效的 MySQL 密码）
3. 执行 `make guard-install`

工作机制：
- 每分钟扫描 NewAPI 容器日志
- 同一渠道累计 3+ 次错误后自动禁用（面板显示禁用）
- 对应容器重启后自动恢复（面板显示恢复）

## 安全提醒

- 不要提交 `.env` 和 `envs/*.env`（含 Cookie 和密码）
- 不要提交 `cookie-cache/` 和 `state/`
- 详见 [SECURITY.md](SECURITY.md)

## 致谢

本项目站在以下项目的肩膀上，感谢原作者的开源贡献：

| 项目 | 作者 | 我们参考了什么 |
|------|------|---------------|
| [Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) | HanaokaYuzu | **核心依赖** — `gemini-webapi` 库，提供 Gemini Web Cookie 认证和对话能力，本项目的 `app/main.py` 直接基于此库封装 |
| [Gemini-FastAPI](https://github.com/Nativu5/Gemini-FastAPI) | Nativu5 | **架构参考** — OpenAI 兼容 API 格式、多账号负载均衡思路、LMDB 会话持久化方案 |
| [Gemi2Api-Server](https://github.com/zhiyu1998/Gemi2Api-Server) | zhiyu1998 | **部署参考** — 轻量化 Docker 部署模式、快速起服的工程结构 |

如果你需要更底层的定制能力，推荐直接使用上述项目：
- 想做二次开发 → [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API)（底座库，功能最全）
- 想开箱即用 → [Nativu5/Gemini-FastAPI](https://github.com/Nativu5/Gemini-FastAPI)（成品服务，多账号池化）
- 想最快跑起来 → [zhiyu1998/Gemi2Api-Server](https://github.com/zhiyu1998/Gemi2Api-Server)（轻量，上手快）

## 许可证

[MIT](LICENSE) — 免费使用、修改、分发，保留署名即可。

---

**🎀 Gemini API OneClick** by WanWan | [GitHub](https://github.com/shleeshlee/gemini-api-oneclick)

如果觉得好用，请给个 Star 支持一下！
