# 🎀 Gemini API OneClick

一键部署 Gemini API 多账号智能网关 — 自动轮询、分组路由、健康检查、Cookie 管理，一个端口搞定。

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

安装向导 5 步完成：

1. **容器数量** — 几个 Gemini 账号就填几个
2. **端口范围** — 容器起始端口（自动检测冲突）
3. **API 密钥** — 客户端调用密钥（可自动生成）
4. **代理设置** — 出站代理（可选）
5. **Gateway 端口** — 统一入口，默认 9880

已有环境重新跑 `install.sh` 可选择更新（保留账号配置）或全新安装。

## 功能一览

- ⚡ **智能网关** — 自动轮询所有容器，故障节点静默跳过，最多 5 次重试
- 🏷️ **分组路由** — 按模型前缀自动分流到不同容器池（如 `pro-gemini-2.0-flash` → `pro` 组）
- 🔍 **健康检查** — 后台每 30 秒检测容器状态，连续 3 次失败自动禁用
- 📊 **管理面板** — 容器状态、请求统计、错误日志、容器测试、日志查看
- 🍪 **Cookie 管理** — 面板内直接部署 Cookie，一键重启单个容器
- 🔖 **账号标识** — 给容器命名（如"工作号""备用号"），服务端存储
- 🔒 **安全加固** — 速率限制、timing-safe 认证、防暴力破解
- ➕ **弹性扩容** — 随时通过 `manage.sh` 新增/删除容器
- 🔄 **一键更新** — 已安装的环境重新跑 `install.sh` 选"更新"即可

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
         [pro 组]     [pro 组]     [默认组]
              │            │            │
              ▼            ▼            ▼
           Gemini Web API (Cookie 认证)
```

- **Gateway** — 宿主机进程，统一接收请求，按分组路由 + 轮询分发到健康容器
- **Container** — 每个容器一个 FastAPI 实例，使用独立的 Gemini Cookie
- **不需要外部负载均衡** — Gateway 自带轮询、故障转移和分组路由

## 分组路由

将容器分成不同的组（如 `pro`、`free`），通过模型名前缀指定走哪个组：

```
请求模型: pro-gemini-2.0-flash
  ↓ Gateway 解析
分组: pro | 实际模型: gemini-2.0-flash
  ↓ 只在 pro 组的容器里轮询
转发到 pro 组的健康容器
```

**使用场景：** 在 NewAPI 等中转站配置不同渠道使用不同前缀，实现账号隔离。未分组的容器接收所有无前缀请求。

面板内管理分组：创建 / 删除 / 重命名 / 批量分配容器。

## API 端点

所有端点通过 Gateway 统一入口访问，请求头带 `Authorization: Bearer 你的API密钥`。

| 端点 | 说明 |
|------|------|
| `POST /v1/chat/completions` | 聊天（OpenAI 兼容，支持流式） |
| `GET /v1/models` | 可用模型列表 |
| `GET /` | 管理面板 |

兼容 OpenAI 格式，可直接接入 SillyTavern、NextChat、NewAPI 等。

## 管理面板

浏览器打开 `http://你的IP:9880`，输入密码登录：

- **容器卡片** — 编号、端口、名称、分组、健康状态一目了然
- **一键测试** — 直接测试容器是否可用，结果显示在按钮上
- **Cookie 部署** — 点击容器卡片填入 Cookie，一键重启生效
- **容器日志** — 面板内查看每个容器的 Docker 日志
- **分组管理** — 创建分组、批量分配容器、重命名、删除
- **请求统计** — 从容器日志读取真实请求数和错误数
- **启用/禁用** — 手动控制容器是否参与轮询
- **模型刷新** — 设置内一键刷新可用模型列表

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
| `make manage` | 容器管理菜单 |
| `make up` | 启动所有容器 |
| `make down` | 停止所有容器 |
| `make restart` | 重启所有容器 |
| `make logs` | 实时查看日志 |
| `make generate` | 重新生成 docker-compose |

## 安全提醒

- Gateway 面板登录有速率限制（每 IP 60 秒内 5 次）
- API 密钥使用 timing-safe 比较，防时序攻击
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

## 超时与容错

请求经过三层超时保护，每层超时都会释放资源并尝试下一个容器：

| 层级 | 生图 | 生文 | 说明 |
|------|------|------|------|
| 容器内部（Gemini API） | 150s | 150s | gemini_webapi 的请求超时，超时后释放连接 |
| Gateway（单容器） | 100s | 120s | 单个容器无响应时跳下一个，超时容器冷却 60s |
| 客户端（Bot 等） | 120s | 120s | 给 Gateway 足够时间完成容器重试 |

**容器冷却机制：** 容器超时后 60 秒内不再接收新请求，防止积压。冷却结束后自动恢复轮询。
