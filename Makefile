SHELL := /bin/bash

.PHONY: install generate up down restart status logs manage

install:
	./scripts/install.sh

generate:
	python3 scripts/generate_compose.py

up: generate
	./scripts/safe-deploy.sh --build

down:
	docker compose -f docker-compose.accounts.yml down

restart: generate
	./scripts/safe-deploy.sh

status:
	./scripts/manage.sh

logs:
	docker compose -f docker-compose.accounts.yml logs --tail 120 -f

manage:
	./scripts/manage.sh
