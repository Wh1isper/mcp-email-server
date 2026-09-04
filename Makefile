CONTAINER_IMAGE ?= mcp-email-server:local
CONTAINER_VERSION ?= $(shell python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')

.PHONY: install
install: ## Install the virtual environment and install the pre-commit hooks
	@echo "Creating virtual environment using uv"
	@uv sync
	@uv run pre-commit install

.PHONY: frontend
frontend: ## Build, test, and stage the embedded management UI
	@uv run python dev/build_frontend.py

.PHONY: frontend-check
frontend-check: ## Verify staged UI assets without Node
	@uv run python dev/build_frontend.py --check

.PHONY: test-browser
test-browser: ## Run the real-browser local management UI E2E
	@cd frontend && npm run test:e2e

.PHONY: verify-dist
verify-dist: ## Verify and smoke-test the exact artifacts already built in dist
	@MCP_EMAIL_SERVER_TEST_DIST_DIR="$(CURDIR)/dist" uv run pytest -q tests/test_packaging.py

.PHONY: check
check: frontend-check ## Run code quality tools
	@echo "Checking lock file consistency with pyproject.toml"
	@uv lock --locked
	@echo "Linting code with pre-commit"
	@uv run pre-commit run -a
	@echo "Checking types with pyright"
	@uv run python -m pyright
	@echo "Checking for obsolete dependencies with deptry"
	@uv run deptry .

.PHONY: test
test: ## Test the code with pytest
	@echo "Testing code with pytest"
	@uv run python -m pytest --cov --cov-config=pyproject.toml --cov-report=xml -vv -s

.PHONY: test-e2e
test-e2e: ## Test the stdio MCP server against local GreenMail SMTP and IMAP services
	@echo "Testing the stdio MCP server against GreenMail"
	@dev/greenmail/run-e2e.sh

.PHONY: build
build: clean-build frontend-check ## Build the wheel and source distribution
	@echo "Creating distribution artifacts"
	@uv build

.PHONY: container
container: ## Build the local runtime container image
	@docker build --tag "$(CONTAINER_IMAGE)" .

.PHONY: container-check
container-check: container ## Build and verify the local runtime container image
	@python3 dev/verify_container.py --image "$(CONTAINER_IMAGE)" --expected-version "$(CONTAINER_VERSION)"

.PHONY: clean-build
clean-build: ## Clean build artifacts
	@echo "Removing build artifacts"
	@uv run python -c "import shutil; from pathlib import Path; shutil.rmtree(Path('dist'), ignore_errors=True)"

.PHONY: publish
publish: ## Publish a release to PyPI
	@echo "Publishing distribution artifacts"
	@uvx twine upload --repository-url https://upload.pypi.org/legacy/ dist/*

.PHONY: build-and-publish
build-and-publish: build publish ## Build and publish

.PHONY: docs-test
docs-test: ## Test whether documentation builds without warnings or errors
	@uv run mkdocs build --strict

.PHONY: docs
docs: ## Build and serve the documentation locally
	@uv run mkdocs serve

.PHONY: help
help:
	@uv run python -c "import re; \
	[[print(f'\033[36m{m[0]:<20}\033[0m {m[1]}') for m in re.findall(r'^([a-zA-Z_-]+):.*?## (.*)$$', open(makefile).read(), re.M)] for makefile in ('$(MAKEFILE_LIST)').strip().split()]"

.PHONY: install-claude-desktop
install-claude-desktop: ## Install the desktop application
	@uv sync
	@python dev/install_claude_desktop.py

.DEFAULT_GOAL := help
