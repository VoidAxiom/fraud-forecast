.PHONY: up down reset migrate psql redis-cli test typecheck lint logs ps

up:
	docker compose up -d postgres redis && bash scripts/wait_for_postgres.sh

down:
	docker compose down

reset:
	docker compose down -v && $(MAKE) up

migrate:
	docker compose run --rm --profile tools app alembic upgrade head

psql:
	docker compose exec postgres psql -U app -d fraud_platform

redis-cli:
	docker compose exec redis redis-cli

test:
	docker compose run --rm --profile tools app pytest tests/ -v

typecheck:
	docker compose run --rm --profile tools app mypy --strict shared/

lint:
	docker compose run --rm --profile tools app sh -c 'ruff check . && ruff format --check .'

logs:
	docker compose logs -f --tail=100

ps:
	docker compose ps
