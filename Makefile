SHELL := /bin/bash

.PHONY: install generate up down restart status logs health manage guard-run guard-install guard-remove

install:
	./scripts/install.sh

generate:
	python3 scripts/generate_compose.py

up: generate
	docker compose -f docker-compose.accounts.yml up -d --build

down:
	docker compose -f docker-compose.accounts.yml down

restart:
	docker compose -f docker-compose.accounts.yml restart

status:
	docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E '^gemini_api_account_' || true

logs:
	docker compose -f docker-compose.accounts.yml logs --tail 120 -f

health:
	./scripts/healthcheck.sh

manage:
	./scripts/manage.sh

guard-run:
	set -a; source .env; set +a; python3 ops/channel_guard.py

guard-install:
	./ops/install_cron.sh

guard-remove:
	./ops/install_cron.sh --remove
