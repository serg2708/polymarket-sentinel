.PHONY: up down build logs pull-models lint test

up:
	docker compose up -d

down:
	docker compose down

build:
	docker compose build

logs:
	docker compose logs -f --tail=100

# Pull the default Ollama models (run once after first `make up`)
pull-models:
	docker compose exec ollama ollama pull qwen2.5:14b-instruct
	docker compose exec ollama ollama pull llama3.1:8b-instruct-q4_K_M

# One-time: copy .env.example → .env (edit before running)
setup:
	@if [ ! -f .env ]; then cp .env.example .env; echo "Created .env — edit it before running 'make up'"; else echo ".env already exists"; fi

lint:
	ruff check packages/

test:
	pytest packages/ -v

db-shell:
	docker compose exec db psql -U postgres -d polysentinel

redis-cli:
	docker compose exec redis redis-cli

# Show recent alerts from DB
recent-alerts:
	docker compose exec db psql -U postgres -d polysentinel \
	  -c "SELECT ts, kind, group_key, edge_bps, status FROM alerts ORDER BY ts DESC LIMIT 20;"
