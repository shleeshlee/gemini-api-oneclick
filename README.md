# 🎀 Gemini API OneClick

一键部署 Gemini API 多账号智能网关，自带轮询负载均衡、健康检查和 Cookie 管理面板。

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

> **👤 作者:** WanWan
> **📦 开源协议:** MIT (免费使用，保留署名)
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

安装向导会引导你完成：

1. 容器数量（几个账号）
2. API 密钥配置
3. 代理设置（可选）
4. Cookie 管理面板（可选）

安装完成后会显示统一 API 入口地址，一个端口搞定所有请求。

## 功能一览

- ⚡ **智能网关** — 自动轮询所有容器，故障节点静默跳过，用户无感
- 🔍 **健康检查** — 后台每 30 秒检测容器状态，Cookie 过期自动禁用
- 📊 **状态面板** — Web UI 查看每个容器的健康状态、请求统计、错误日志
- 🏷️ **账号标识** — 给每个容器命名（如"工作号""备用号"），服务端存储，换浏览器不丢
- 🍪 **Cookie 管理** — Web UI 管理每个账号的 Cookie，填完一键重启单个容器
- ➕ **弹性扩容** — 随时通过 `manage.sh` 新增容器
- 🔄 **一键更新** — 已安装的环境重新跑 `install.sh` 选"更新"即可

## 架构

```
                    ┌─────────────────────┐
    用户请求 ──────>│   Gateway (总入口)    │
                    │  智能轮询 + 健康检查   │
                    │  + 状态面板           │
                    └──────┬──────────────┘
                           │ 自动分发
              ┌────────────┼────────────┐
              ▼            ▼            ▼
         Container 1  Container 2  Container N
         (账号 1)     (账号 2)     (账号 N)
              │            │            │
              ▼            ▼            ▼
           Gemini Web API (Cookie 认证)
```

- **Gateway** — 跑在宿主机上，统一接收所有请求，自动分发到健康的容器
- **Container** — 每个容器跑一个独立的 FastAPI 实例，使用各自的 Gemini Cookie
- **不需要外部负载均衡** — Gateway 自己管轮询和故障转移

## API 端点

安装完成后，所有端点都通过 Gateway 总入口访问：

| 端点 | 说明 |
|------|------|
| `/v1/chat/completions` | 聊天（OpenAI 兼容格式） |
| `/v1/models` | 模型列表 |
| `/` | 状态面板 |

请求头带 `Authorization: Bearer 你的API密钥` 即可。支持 OpenAI 兼容格式，可直接接入酒馆、Kelivo、NewAPI 等。

## 状态面板

浏览器打开 Gateway 地址即可查看：

- 每个容器的健康状态（正常/异常/已禁用）
- 请求统计和错误日志
- 点击容器名称可编辑账号标识（存在服务端，换浏览器不丢）
- 手动启用/禁用容器
- 一键刷新健康检查

Cookie 管理功能已集成在 Gateway 面板中，点击容器卡片即可部署 Cookie。

## 容器管理

```bash
bash scripts/manage.sh
# 或
make manage
```

- **[1] 添加容器** — 指定数量，自动创建 env、生成 compose、增量启动
- **[2] 删除容器** — 选择编号，停止容器、删除配置
- **[3] 查看状态** — 容器运行状态 + Gateway 状态
- **[4] 完整卸载** — 清理容器和配置（保留 envs/ 防误删）

## 常用命令

| 命令 | 说明 |
|------|------|
| `make install` | 运行交互式安装 |
| `make manage` | 容器管理菜单（添加/删除/状态/卸载） |
| `make up` | 启动所有容器 |
| `make down` | 停止所有容器 |
| `make restart` | 重启所有容器 |
| `make logs` | 实时查看日志 |
| `make generate` | 重新生成 docker-compose |

## 安全提醒

- 不要提交 `.env` 和 `envs/*.env`（含 Cookie 和密码）
- 不要提交 `cookie-cache/` 和 `state/`
- 详见 [SECURITY.md](SECURITY.md)

## 致谢

| 项目 | 作者 | 参考内容 |
|------|------|---------|
| [Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) | HanaokaYuzu | **核心依赖** — `gemini-webapi` 库，Gemini Web Cookie 认证和对话能力 |
| [Gemini-FastAPI](https://github.com/Nativu5/Gemini-FastAPI) | Nativu5 | **架构参考** — OpenAI 兼容 API 格式、多账号负载均衡思路 |
| [Gemi2Api-Server](https://github.com/zhiyu1998/Gemi2Api-Server) | zhiyu1998 | **部署参考** — 轻量化 Docker 部署模式 |

## 许可证

[MIT](LICENSE) — 免费使用、修改、分发，保留署名即可。

---

**🎀 Gemini API OneClick** by WanWan | [GitHub](https://github.com/shleeshlee/gemini-api-oneclick)

觉得好用的话，给个 Star 支持一下！
