.PHONY: up down reset migrate psql redis-cli test typecheck lint logs ps seed seed-small

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

seed:
	bash scripts/seed.sh 1.0 8 42

seed-small:
	bash scripts/seed.sh 0.1 4 42

TRAIN_RUN_ID ?= run_$(shell date +%Y%m%d_%H%M%S)
TRAIN_INPUT_PARQUET ?= ml/data/training/latest.parquet
TRAIN_OUTPUT_DIR ?= ml/data/transformed

# Requires data from P5-B.
.PHONY: train
train:
	docker compose --profile tools run --rm --build app python -m ml.transform.run_transform \
		--input-parquet $(TRAIN_INPUT_PARQUET) \
		--output-dir $(TRAIN_OUTPUT_DIR) \
		--run-id $(TRAIN_RUN_ID) \
		$(TRAIN_ARGS)
