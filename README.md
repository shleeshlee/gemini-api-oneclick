# Gemini API OneClick v0.1.0

One-click deployment for Gemini API multi-account gateway, with Cookie Manager panel and channel guard.

[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Version](https://img.shields.io/badge/version-0.1.0-green.svg)]()

> **Author:** WanWan
> **License:** MIT (free to use, keep attribution)
> **Disclaimer:** This project is completely free and open source. If you paid for it, you got scammed!

## Quick Start

```bash
git clone https://github.com/shleeshlee/gemini-api-oneclick.git
cd gemini-api-oneclick
bash scripts/install.sh
```

The interactive installer will guide you through:

1. Number of containers (accounts)
2. API key configuration
3. Proxy settings
4. Cookie Manager panel setup
5. Channel guard (optional, for NewAPI)

## Features

- **Interactive Installer** - AccBox-style wizard, handles everything
- **Cookie Manager** - Web UI for managing cookies per container (deploy + restart)
- **Channel Guard** - Auto-disable/recover channels on NewAPI integration
- **Elastic Scaling** - Add containers anytime via `manage.sh`
- **One-click Update** - Re-run `install.sh` on existing install to update

## Container Management

```bash
bash scripts/manage.sh
# or
make manage
```

Menu options:
- **Add containers** - Create new accounts, regenerate compose, start incrementally
- **View status** - Container status + health check
- **Full uninstall** - Clean removal (preserves envs/)

## Cookie Manager Panel

After installation, access the Cookie Manager at `http://YOUR_IP:9880` (default port).

Features:
- View all accounts and container status
- Edit cookies per account
- One-click save + restart
- Local notes per account (stored in browser)
- Export account summary

## Architecture

```
Port 8001  ──> Container 1 (account1.env) ──> Gemini API
Port 8002  ──> Container 2 (account2.env) ──> Gemini API
...
Port 800N  ──> Container N (accountN.env) ──> Gemini API

Port 9880  ──> Cookie Manager (host, systemd)
```

Each container runs an independent FastAPI instance with its own Gemini cookie. Your load balancer (e.g., NewAPI) distributes requests across ports.

## Makefile Commands

| Command | Description |
|---------|-------------|
| `make install` | Run interactive installer |
| `make generate` | Regenerate docker-compose |
| `make up` | Start all containers |
| `make down` | Stop all containers |
| `make restart` | Restart all containers |
| `make status` | Show container status |
| `make logs` | Tail container logs |
| `make health` | Run health check |
| `make manage` | Container management menu |
| `make guard-run` | Run channel_guard once |
| `make guard-install` | Install guard cron job |
| `make guard-remove` | Remove guard cron job |

## Channel Guard (Optional)

Integrates with NewAPI panel to auto-disable channels on repeated errors:

1. Set `ENABLE_CHANNEL_GUARD=true` in `.env`
2. Configure `NEWAPI_DB_PASS` with valid MySQL password
3. Run `make guard-install`

Behavior:
- Scans NewAPI container logs every minute
- Disables channel after 3+ consecutive errors (configurable)
- Auto-recovers when container restarts

## Security

- Never commit `.env` or `envs/*.env` (contains cookies/passwords)
- Never commit `cookie-cache/` or `state/`
- See [SECURITY.md](SECURITY.md) for full guidelines

## Credits

本项目站在以下项目的肩膀上，感谢原作者的开源贡献：

| Project | Author | What we used |
|---------|--------|-------------|
| [Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) | HanaokaYuzu | **核心依赖** - `gemini-webapi` 库，提供 Gemini Web Cookie 认证和对话能力，本项目的 `app/main.py` 直接基于此库封装 |
| [Gemini-FastAPI](https://github.com/Nativu5/Gemini-FastAPI) | Nativu5 | **架构参考** - OpenAI 兼容 API 格式、多账号负载均衡思路、LMDB 会话持久化方案 |
| [Gemi2Api-Server](https://github.com/zhiyu1998/Gemi2Api-Server) | zhiyu1998 | **部署参考** - 轻量化 Docker 部署模式、快速起服的工程结构 |

如果你需要更底层的定制能力，推荐直接使用上述项目：
- 想做二次开发 → [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API)（底座库，功能最全）
- 想开箱即用 → [Nativu5/Gemini-FastAPI](https://github.com/Nativu5/Gemini-FastAPI)（成品服务，多账号池化）
- 想最快跑起来 → [zhiyu1998/Gemi2Api-Server](https://github.com/zhiyu1998/Gemi2Api-Server)（轻量，上手快）

## License

[MIT](LICENSE) - Free to use, modify, and distribute. Keep attribution.

---

**Gemini API OneClick** by WanWan | [GitHub](https://github.com/shleeshlee/gemini-api-oneclick)

If this project helped you, please give it a Star!
