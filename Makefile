# ─── Deploy Service Makefile ────────────────────────────────────────────────
# 前置條件：
#   1. uv 已安裝且在 PATH 上 (curl -LsSf https://astral.sh/uv/install.sh | sh)
#      ├─ macOS/Linux 預設裝在 ~/.local/bin/，安裝 script 會把它加進 shell profile
#      ├─ 若 `which uv` 找不到，請手動加：export PATH="$HOME/.local/bin:$PATH"
#      └─ CI runner 通常用 actions/setup-uv 或在 job step 預先安裝
#   2. 所有 `uv run *` 會自動使用專案根目錄的 .venv/ — 不需要手動 activate
UV     := uv
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
	@echo "  make start        啟動伺服器（使用 .env，不帶 APP_ENV）"
	@echo "  make dev          啟動開發伺服器（使用 .env + .env.dev，熱重載）"
	@echo "  make prod         啟動生產伺服器（使用 .env + .env.prod）"
	@echo "  make inventory-api 啟動本機假 Inventory API（port 9001）"
	@echo ""
	@echo "  make test         執行測試（不含 e2e；日常開發、CI 用這個）"
	@echo "  make test-all     執行全部測試（含 e2e；需先 redis-up + setup-ssh-nodes）"
	@echo "  make test-e2e     只執行 e2e 測試（需 Redis + docker SSH nodes）"
	@echo "  make test-ci      CI 別名（等同 make test）"
	@echo "  make test-unit    只執行 unit tests"
	@echo "  make test-int     只執行 integration tests"
	@echo "  make test-cov     執行測試並顯示覆蓋率（不含 e2e）"
	@echo ""
	@echo "  make hash p=<密碼>  快速 hash 一個密碼"
	@echo "  make clean        刪除 .venv、快取"
	@echo ""

# ─── Setup ───────────────────────────────────────────────────────────────────
.PHONY: install
install:
	$(UV) sync --group dev

.PHONY: setup-ssh-nodes
setup-ssh-nodes:
	chmod +x scripts/setup_ssh_nodes.sh && ./scripts/setup_ssh_nodes.sh

# ─── Redis Setup ─────────────────────────────────────────────────────────────
.PHONY: redis-up
redis-up:
	docker-compose up -d

.PHONY: redis-down
redis-down:
	docker-compose down -v

# ─── Run ─────────────────────────────────────────────────────────────────────
# start: 只讀 .env（不設 APP_ENV，適合生產部署只有單一 .env 的情境）
.PHONY: start
start:
	$(UV) run uvicorn app.main:app --host 0.0.0.0 --port 8000

# dev: 讀 .env + .env.dev（.env.dev 的值會覆蓋 .env）
.PHONY: dev
dev: redis-up
	APP_ENV=dev $(UV) run uvicorn app.main:app --reload --port 8001

# prod: 讀 .env + .env.prod（.env.prod 的值會覆蓋 .env）
.PHONY: prod
prod:
	APP_ENV=prod $(UV) run uvicorn app.main:app --host 0.0.0.0 --port 8000

# inventory-api: 啟動本機假 Inventory API (port 9001)
.PHONY: inventory-api
inventory-api:
	APP_ENV=dev $(UV) run uvicorn fake-api.main:app --reload --port 9001

# ─── Test ────────────────────────────────────────────────────────────────────
# 測試分層：
#   tests/unit/         純邏輯，無外部依賴
#   tests/integration/  TestClient + mock，無外部依賴（CI 安全）
#   tests/e2e/          需要真 Redis + docker SSH nodes，預設跳過
#                       （e2e 檔案內帶 skipif；本層另外用 -m 過濾雙保險）
#
# 推薦用法：
#   本地全跑（含 e2e）：     make test-all     ← 需先 make redis-up + make setup-ssh-nodes
#   本地不跑 e2e：           make test         ← 日常開發迭代
#   只跑 e2e：               make test-e2e     ← 確認上下游整合
#   CI/CD 建議：             make test-ci      ← 等同 make test，明確語意

# test: 不跑 e2e（單元 + 純 integration），不需要外部依賴
.PHONY: test
test:
	APP_ENV=test $(PYTEST) tests/ -v -m "not e2e"

# test-ci: CI 專用別名，語意明確 — 等同 make test
.PHONY: test-ci
test-ci: test

# test-all: 跑全部，包含 e2e；需要先起 Redis + SSH nodes
.PHONY: test-all
test-all:
	APP_ENV=test RUN_E2E=1 $(PYTEST) tests/ -v

# test-e2e: 只跑 e2e；需要先起 Redis + SSH nodes
.PHONY: test-e2e
test-e2e:
	APP_ENV=test RUN_E2E=1 $(PYTEST) tests/e2e/ -v

.PHONY: test-unit
test-unit:
	APP_ENV=test $(PYTEST) tests/unit/ -v

.PHONY: test-int
test-int:
	APP_ENV=test $(PYTEST) tests/integration/ -v

.PHONY: test-cov
test-cov:
	APP_ENV=test $(PYTEST) tests/ -v --tb=short -m "not e2e" \
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
