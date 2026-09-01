.PHONY: dev build backend frontend clean up down help deps migrate start stop restart deploy retire-legacy-worker

BACKEND_ENTRYPOINT := uv run python scripts/backend_entrypoint.py
FRONTEND_DIR := frontend-v2
FRONTEND_PORT := 3000

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-15s\033[0m %s\n", $$1, $$2}'

backend: retire-legacy-worker ## Start backend dev server (foreground, with reload)
	cd backend && $(BACKEND_ENTRYPOINT) --reload --host 0.0.0.0 --port 8080

frontend: ## Start frontend dev server (foreground)
	cd $(FRONTEND_DIR) && npm run dev -- --host 0.0.0.0 --port $(FRONTEND_PORT)

dev: retire-legacy-worker ## Start backend + V2 frontend for development (foreground)
	@echo "Applying database migrations..."
	cd backend && $(BACKEND_ENTRYPOINT) --migrate-only
	@echo "Starting backend..."
	cd backend && $(BACKEND_ENTRYPOINT) --skip-migrate --reload --host 0.0.0.0 --port 8080 &
	@echo "Starting frontend..."
	cd $(FRONTEND_DIR) && npm run dev -- --host 0.0.0.0 --port $(FRONTEND_PORT)

start: retire-legacy-worker ## Start backend + V2 frontend (background)
	@echo "=== Stopping old processes ==="
	@-pkill -f "uvicorn main:app" 2>/dev/null || true
	@-pkill -f "vite" 2>/dev/null || true
	@sleep 1
	@-kill -9 $$(lsof -ti :8080) 2>/dev/null || true
	@-kill -9 $$(lsof -ti :$(FRONTEND_PORT)) 2>/dev/null || true
	@sleep 1
	@echo "=== Installing dependencies ==="
	cd backend && uv sync
	cd $(FRONTEND_DIR) && npm install --silent
	@echo "=== Applying database migrations ==="
	cd backend && $(BACKEND_ENTRYPOINT) --migrate-only
	@echo "=== Starting backend (port 8080) ==="
	cd backend && nohup $(BACKEND_ENTRYPOINT) --skip-migrate --host 0.0.0.0 --port 8080 > /tmp/openbox-backend.log 2>&1 &
	@echo "=== Starting frontend (port $(FRONTEND_PORT)) ==="
	cd $(FRONTEND_DIR) && nohup npm run dev -- --host 0.0.0.0 --port $(FRONTEND_PORT) > /tmp/openbox-frontend.log 2>&1 &
	@sleep 5
	@echo "=== Health Check ==="
	@curl -s -o /dev/null -w "Backend:  HTTP %{http_code}\n" http://localhost:8080/health 2>/dev/null || echo "Backend:  not ready"
	@curl -s -o /dev/null -w "Frontend: HTTP %{http_code}\n" http://localhost:$(FRONTEND_PORT)/ 2>/dev/null || echo "Frontend: not ready"
	@echo "=== Logs ==="
	@echo "  tail -f /tmp/openbox-backend.log"
	@echo "  tail -f /tmp/openbox-frontend.log"

stop: ## Stop backend + V2 frontend
	@echo "=== Stopping backend & frontend ==="
	@-pkill -f "uvicorn main:app" 2>/dev/null || true
	@-pkill -f "vite" 2>/dev/null || true
	@sleep 1
	@-kill -9 $$(lsof -ti :8080) 2>/dev/null || true
	@-kill -9 $$(lsof -ti :$(FRONTEND_PORT)) 2>/dev/null || true
	@echo "=== All stopped ==="

restart: stop start ## Restart backend + V2 frontend

deploy: ## Pull latest code and restart backend + V2 frontend
	git pull
	$(MAKE) start

build: ## Pull local infrastructure images (Agent execution stays on WUYING)
	docker compose pull

up: retire-legacy-worker ## Start local PostgreSQL/Redis/Azurite only
	docker compose up -d --remove-orphans

down: ## Stop all services (docker-compose)
	docker compose down --remove-orphans

clean: ## Stop local infrastructure and remove its data volumes
	docker compose down --volumes --remove-orphans 2>/dev/null || true

install: ## Install all dependencies
	cd backend && uv sync --extra test
	cd $(FRONTEND_DIR) && npm install

deps: ## Start dev dependencies (PG + Redis + Azurite)
	docker compose -f docker-compose.dev.yml up -d

deps-down: ## Stop dev dependencies
	docker compose -f docker-compose.dev.yml down

migrate: retire-legacy-worker ## Run database migrations
	cd backend && $(BACKEND_ENTRYPOINT) --migrate-only

retire-legacy-worker: ## Remove this checkout's obsolete SkillJob Compose worker before DB migration
	@containers=$$(docker ps -aq \
		--filter "label=com.docker.compose.project.working_dir=$(CURDIR)" \
		--filter "label=com.docker.compose.service=backend-worker" 2>/dev/null); \
	if [ -n "$$containers" ]; then \
		echo "Removing retired SkillJob worker container(s) before migration..."; \
		docker rm -f $$containers; \
	fi

test: ## Run backend tests
	cd backend && uv run pytest tests/ -v

test-isolation: ## Run multi-user isolation tests
	cd backend && uv run pytest tests/integration/test_isolation.py -v
