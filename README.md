# Deploy Service

FastAPI 服務，遵循分層架構（Layered Architecture）並採用 Clean Code 原則：

```
router → service → repository (interface) → infrastructure (JSON / DB)
```

---

## 快速開始（使用 uv）

```bash
# 1. 安裝 uv（若尚未安裝）
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env

# 2. 安裝所有依賴（含 dev 工具）
uv sync --group dev

# 3. 啟動開發伺服器
APP_ENV=dev uv run uvicorn app.main:app --reload --port 8000
```

API 文件（dev 模式）：http://localhost:8000/docs

---

## 專案結構

```
deploy-service/
├── app/
│   ├── main.py                        # App factory + lifespan
│   ├── core/
│   │   ├── config.py                  # Pydantic Settings（多環境）
│   │   ├── security.py                # JWT + bcrypt
│   │   ├── dependencies.py            # get_current_user(scopes)
│   │   ├── exceptions.py              # BaseAppException hierarchy
│   │   └── logging.py                 # RequestIdMiddleware
│   ├── domain/
│   │   └── models.py                  # 所有 Pydantic models
│   ├── repositories/
│   │   ├── user_repository.py         # Abstract interface（DIP）
│   │   └── json_user_repository.py    # JSON 實作
│   ├── services/
│   │   └── auth_service.py            # 業務邏輯
│   └── api/
│       ├── router.py                  # 頂層路由
│       └── v1/
│           └── auth.py                # Auth 端點
├── data/
│   └── users.json                     # 帳號/密碼/scopes 儲存
├── tests/
│   ├── conftest.py
│   ├── unit/
│   ├── integration/
│   └── e2e/
├── .env.dev / .env.prod / .env.test
├── pyproject.toml
└── Dockerfile
```

---

## API 端點

| Method | Path | Auth | 說明 |
|--------|------|------|------|
| `POST` | `/token` | ❌ | OAuth2 登入，取得 JWT |
| `GET` | `/api/v1/auth/verify` | ✅ | 驗證 token 合法性 |
| `POST` | `/api/v1/auth/hash-password` | ❌ | 產生 bcrypt hash |
| `GET` | `/api/v1/auth/my-scopes` | ✅ | 查看目前 token 的 scopes |

### 登入

```bash
curl -X POST http://localhost:8000/token \
  -d "username=admin&password=secret" \
  -H "Content-Type: application/x-www-form-urlencoded"
```

### 使用 token

```bash
TOKEN="<上面取得的 access_token>"

# 驗證
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/auth/verify

# 查 scopes
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/api/v1/auth/my-scopes
```

### 取得密碼 hash（換密碼用）

```bash
curl -X POST http://localhost:8000/api/v1/auth/hash-password \
  -H "Content-Type: application/json" \
  -d '{"password": "mynewpassword"}'
# → 將 hashed_password 貼入 data/users.json
```

---

## 帳號與 Scopes 設定

編輯 `data/users.json`：

```json
[
  {
    "account": "admin",
    "hashed_password": "<用 /hash-password 取得>",
    "scopes": ["deploy_api", "vm_api"]
  }
]
```

預設帳號密碼：`admin` / `secret`（請立即換密碼！）

---

## 保護端點範例（Scope 驗證）

```python
from app.core.dependencies import get_current_user
from app.domain.models import User

@router.get("/deploy")
def deploy(user: User = Depends(get_current_user(["deploy_api"]))):
    ...
```

---

## 環境設定

| 檔案 | 用途 |
|------|------|
| `.env.dev` | 開發（`APP_ENV=dev`，`DEBUG=true`） |
| `.env.prod` | 生產（`APP_ENV=prod`，需替換 `SECRET_KEY`） |
| `.env.test` | 測試（短 token 過期時間，fixture 資料） |

---

## 測試環境與 SSH Nodes 快速架設

本專案配置了一個專屬的本地開發節點叢集 (基於 ubuntu 的 ssh_node_1 與 ssh_node_2) 來讓你安全的測試 SSH Command API (`/api/v1/command/execution`)。

### 1. 初始化與啟動節點

透過下方提供的單一指令，能一鍵完成：Ed25519 憑證 (CA) 與金鑰的生成、Base64 加密注入 `.json` 配置檔，以及背景啟動 Docker 容器。

```bash
make setup-ssh-nodes
```

*執行完成後，Node 1 會映射到 `localhost:2222`，Node 2 會映射到 `localhost:2223`。*

### 2. 手動連線進入 Node 內部除錯

你可以自由選擇「透過產生好的 SSH 金鑰登入」或是「直接透過 Docker 掛載進去」。以下為操作指令範例：

#### 方式一：使用方才生成的 SSH 金鑰直接連線 (推薦)
藉由剛剛產生的私鑰登入，可以確保憑證驗證與服務行為的一致性。

**進入 Node 1 (Port 2222，使用 CA 憑證驗證)：**
```bash
ssh -i data/ssh_keys/client_ca -p 2222 root@localhost -o StrictHostKeyChecking=no
```
*(補充：SSH 客戶端會自動在該目錄配對同名的 `client_ca-cert.pub` 作為憑證登入)*

**進入 Node 2 (Port 2223，使用一般私鑰驗證)：**
```bash
ssh -i data/ssh_keys/client_key -p 2223 root@localhost -o StrictHostKeyChecking=no
```

#### 方式二：透過 Docker Exec 繞過 SSH 直接進入
如果你只是單純想查看背景執行狀態，可以直接用 Docker 終端機掛載進入：
```bash
docker exec -it ssh_node_1 bash
# 或者
docker exec -it ssh_node_2 bash
```

---

## 執行測試

```bash
# 全部測試
APP_ENV=test uv run pytest tests/ -v

# 單元測試
APP_ENV=test uv run pytest tests/unit/ -v

# 整合測試
APP_ENV=test uv run pytest tests/integration/ -v
```

---

## Docker

```bash
docker build -t deploy-service .
docker run -p 8000:8000 -e APP_ENV=prod -e SECRET_KEY=<your-secret> deploy-service
```
