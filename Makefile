.PHONY: install test lint run run-paper

install:
	python -m pip install -e ".[dev]"

test:
	pytest tests/ -v --cov=copybot --cov-report=term-missing

test-unit:
	pytest tests/unit/ -v

test-integration:
	pytest tests/integration/ -v

lint:
	ruff check copybot/ tests/
	ruff format --check copybot/ tests/

format:
	ruff format copybot/ tests/

run:
	python -m copybot.main

run-paper:
	python -m copybot.main --mode paper
