.PHONY: up down reset migrate psql redis-cli test typecheck lint logs ps

up:
	docker compose up -d postgres redis && bash scripts/wait_for_postgres.sh

down:
	docker compose --profile tools down

reset:
	docker compose --profile tools down -v && $(MAKE) up

migrate:
	@ [ -f db/alembic.ini ] || { echo "ERROR: db/alembic.ini not found. Run 'make migrate' after P1-B (VOI-142) lands."; exit 1; }
	docker compose --profile tools run --rm app alembic -c db/alembic.ini upgrade head

psql:
	docker compose exec postgres psql -U app -d fraud_platform

redis-cli:
	docker compose exec redis redis-cli

test:
	@ [ -d tests ] || { echo "ERROR: tests/ directory not found. Run 'make test' after P1-E (VOI-145) lands."; exit 1; }
	docker compose --profile tools run --rm --build app pytest tests/ -v

typecheck:
	@ [ -d shared ] || { echo "ERROR: shared/ directory not found. Run 'make typecheck' after P1-C (VOI-143) lands."; exit 1; }
	docker compose --profile tools run --rm --build app mypy --strict shared/

lint:
	docker compose --profile tools run --rm --build app sh -c 'ruff check . && ruff format --check .'

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps
