SHELL := /bin/bash

ARCH ?= $(shell awk -F= '/^WORKER_MODE=/{gsub(/[[:space:]]/, "", $$2); if ($$2=="true" || $$2=="1" || $$2=="yes") print "worker"; else print "accounts"}' .env 2>/dev/null || true)
ARCH := $(if $(ARCH),$(ARCH),worker)

ifeq ($(ARCH),worker)
  COMPOSE_FILE := docker-compose.worker.yml
else
  COMPOSE_FILE := docker-compose.accounts.yml
endif

.PHONY: install generate up down restart status logs manage uninstall print-arch config

install:
	./scripts/install.sh

generate:
ifeq ($(ARCH),accounts)
	python3 scripts/generate_compose.py
else
	@echo "ARCH=worker: skip generate"
endif

up: generate
ifeq ($(ARCH),accounts)
	./scripts/safe-deploy.sh --build
else
	docker compose -f $(COMPOSE_FILE) up -d --build
endif

down:
	docker compose -f $(COMPOSE_FILE) down

restart: generate
ifeq ($(ARCH),accounts)
	./scripts/safe-deploy.sh
else
	docker compose -f $(COMPOSE_FILE) up -d --force-recreate
endif

status:
	./scripts/manage.sh status

logs:
	docker compose -f $(COMPOSE_FILE) logs --tail 120 -f

manage:
	./scripts/manage.sh

uninstall:
	./scripts/uninstall.sh

print-arch:
	@echo $(ARCH)

config:
	docker compose -f $(COMPOSE_FILE) config
