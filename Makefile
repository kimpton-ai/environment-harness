.PHONY: setup check build viewer demo
setup:
	uv sync --extra server
check:
	uv run --no-sync pytest -q
	uv run --no-sync ruff check src tests scripts examples
	uv run --no-sync python scripts/build_contracts.py --check
	npm run typecheck --prefix packages/typescript
	npm test --prefix packages/typescript
build:
	uv build
	uv run --no-sync python scripts/check_distribution.py
viewer:
	npm ci --prefix packages/typescript
	uv run --no-sync python scripts/build_viewer.py
demo:
	uv run --no-sync environment-harness --store .local/demo quickstart --turns 10
	uv run --no-sync environment-harness --store .local/demo serve
