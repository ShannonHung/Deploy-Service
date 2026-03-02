# ─── Deploy Service Makefile ────────────────────────────────────────────────
# 前置條件：uv 已安裝 (curl -LsSf https://astral.sh/uv/install.sh | sh)

UV     := source $$HOME/.local/bin/env && uv
PYTHON := $(UV) run python
PYTEST := $(UV) run pytest

.DEFAULT_GOAL := help

# ─── Help ────────────────────────────────────────────────────────────────────
.PHONY: help
help:
	@echo ""
	@echo "  Deploy Service — 常用指令"
	@echo ""
	@echo "  make install      安裝所有依賴（含 dev）到 .venv/"
	@echo "  make dev          啟動開發伺服器（熱重載，port 8000）"
	@echo "  make prod         啟動生產伺服器（無熱重載）"
	@echo ""
	@echo "  make test         執行全部測試"
	@echo "  make test-unit    只執行 unit tests"
	@echo "  make test-int     只執行 integration tests"
	@echo "  make test-cov     執行全部測試並顯示覆蓋率"
	@echo ""
	@echo "  make hash p=<密碼>  快速 hash 一個密碼"
	@echo "  make clean        刪除 .venv、快取"
	@echo ""

# ─── Setup ───────────────────────────────────────────────────────────────────
.PHONY: install
install:
	$(UV) sync --group dev

# ─── Run ─────────────────────────────────────────────────────────────────────
.PHONY: dev
dev:
	APP_ENV=dev $(UV) run uvicorn app.main:app --reload --port 8000

.PHONY: prod
prod:
	APP_ENV=prod $(UV) run uvicorn app.main:app --host 0.0.0.0 --port 8000

# ─── Test ────────────────────────────────────────────────────────────────────
.PHONY: test
test:
	APP_ENV=test $(PYTEST) tests/ -v

.PHONY: test-unit
test-unit:
	APP_ENV=test $(PYTEST) tests/unit/ -v

.PHONY: test-int
test-int:
	APP_ENV=test $(PYTEST) tests/integration/ -v

.PHONY: test-cov
test-cov:
	APP_ENV=test $(PYTEST) tests/ -v --tb=short \
		--cov=app --cov-report=term-missing

# ─── Utils ───────────────────────────────────────────────────────────────────
# 用法：make hash p=yourpassword
.PHONY: hash
hash:
	@$(PYTHON) -c \
		"import bcrypt; print(bcrypt.hashpw('$(p)'.encode(), bcrypt.gensalt()).decode())"

.PHONY: clean
clean:
	rm -rf .venv .pytest_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "清理完成。"
