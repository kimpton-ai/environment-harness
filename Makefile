.PHONY: setup check security build viewer demo
setup:
	uv sync --extra server
	npm ci --ignore-scripts --prefix packages/typescript
check:
	uv run --no-sync pytest --cov --cov-branch --cov-report=term-missing --cov-report=json
	uv run --no-sync python scripts/check_coverage.py
	uv run --no-sync ruff format --check src tests scripts examples
	uv run --no-sync ruff check src tests scripts examples
	uv run --no-sync pyright
	uv run --no-sync python scripts/build_contracts.py --check
	uv run --no-sync python scripts/build_viewer.py --check
	npm run typecheck --prefix packages/typescript
	npm test --prefix packages/typescript
	uv run --no-sync python scripts/check_repository.py
security:
	uv run --no-sync python scripts/check_repository.py --full
	uv export --frozen --all-extras --no-dev --no-emit-project --no-hashes | uv run --no-sync pip-audit -r /dev/stdin
	npm audit --audit-level=high --prefix packages/typescript
build:
	uv build
	cd packages/typescript && npm pack --ignore-scripts --pack-destination ../../dist
	uv run --no-sync python scripts/check_distribution.py
viewer:
	npm ci --ignore-scripts --prefix packages/typescript
	uv run --no-sync python scripts/build_viewer.py
demo:
	uv run --no-sync environment-harness --store .local/demo quickstart --turns 10
	uv run --no-sync environment-harness --store .local/demo serve
