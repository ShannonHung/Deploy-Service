# SSH Command Execution — Architecture & Design

> 本文件描述 `deploy-service` 中 SSH 遠端指令執行功能的核心設計理念，涵蓋安全防注入機制、進程生命週期管理、斷線指令處理、以及結果輪詢快取等關鍵架構。

---

## 目錄

1. [系統總覽](#1-系統總覽)
2. [請求生命週期](#2-請求生命週期)
3. [白名單與管線設計](#3-白名單與管線設計)
4. [Anti-Injection 防注入架構](#4-anti-injection-防注入架構)
5. [進程群組追蹤與 Timeout Kill 機制](#5-進程群組追蹤與-timeout-kill-機制)
6. [Fire-and-Forget 斷線指令（如 Reboot）](#6-fire-and-forget-斷線指令如-reboot)
7. [Running Pool 與 Results Pool](#7-running-pool-與-results-pool)
8. [Graceful Shutdown（優雅關機）](#8-graceful-shutdown優雅關機)
9. [結構化日誌](#9-結構化日誌)
10. [附錄：核心檔案索引](#10-附錄核心檔案索引)

---

## 1. 系統總覽

本模組讓授權使用者透過 REST API 在遠端主機上執行**預定義的白名單指令**。不允許任意字串直接傳入 Shell，所有可執行的指令皆須事先在 JSON 設定檔中以管線（Pipeline）形式定義。

```
Client ──HTTP──▶ FastAPI ──SSH──▶ Remote Node
                   │                   │
                   │  asyncssh.connect  │
                   │──────────────────▶│
                   │  create_process    │
                   │──────────────────▶│
                   │◀── stdout/stderr ──│
```

**核心約束**：
- 使用者**無法自行決定要執行什麼指令**，只能從白名單中選取。
- 使用者**唯一能控制的**是白名單中預留的參數（argument），而這些參數受到多層驗證保護。

---

## 2. 請求生命週期

一個指令從接收到完成，會依序經過以下階段。每個階段對應 `CommandService` 中的一個獨立方法，遵循 Single Responsibility Principle：

```
execute_command()          ← 頂層 Orchestrator
 ├─ _prepare_execution()   ← 解析白名單、驗證參數、載入 SSH 設定
 ├─ _build_pipeline()      ← 將 {placeholder} 替換為實際參數值，產出 List[List[str]]
 ├─ _connect()             ← 建立 SSH 連線
 └─ 分流 ──┬─ _handle_fire_and_forget()   ← disconnects_ssh=true
           └─ _handle_async_execution()   ← 一般指令（背景執行 + 超時控制）
                ├─ _execute_pipeline()     ← 建立 Process、擷取 PGID
                ├─ _collect_output()       ← 收集 stdout + stderr
                └─ _store_result()         ← 寫入 results pool
```

所有階段所需的上下文資訊統一封裝在 `ExecutionContext` Dataclass 中，避免冗長的函式參數傳遞：

```python
@dataclass
class ExecutionContext:
    username: str
    request_id: str
    command_name: str
    raw_request: CommandExecutionRequest
    cmd_config: CommandWhitelistConfig
    ssh_config: SSHConnectionConfig
    conn: Optional[asyncssh.SSHClientConnection] = None
    pipeline_cmds: List[List[str]] = field(default_factory=list)
```

---

## 3. 白名單與管線設計

### 設定檔結構

每個角色擁有獨立的白名單設定檔，例如 `data/allow-commands-admin.json`：

```json
{
  "name": "admin",
  "allow_commands": [
    {
      "command_name": "list_file",
      "description": "列出檔案並篩選關鍵字",
      "disconnects_ssh": false,
      "killable": true,
      "pipeline": [
        { "command": ["ls", "-al"] },
        { "command": ["grep", "{key_word}"] }
      ],
      "arguments": [
        {
          "name": "key_word",
          "type": "string",
          "validation_regex": "^[a-zA-Z0-9._-]+$"
        }
      ]
    }
  ]
}
```

### 關鍵欄位說明

| 欄位 | 用途 |
|---|---|
| `pipeline` | 依序執行的指令陣列，每個 step 是一個 `command: List[str]`。多個 step 會透過 Python 管線串接（stdin → stdout）。 |
| `arguments` | 使用者可替換的 `{placeholder}` 參數定義。每一個必須包含 `validation_regex` 來限縮合法輸入範圍。 |
| `disconnects_ssh` | 設為 `true` 時，系統預期指令會主動切斷 SSH 連線（如 `reboot`），走「Fire-and-Forget」路徑。 |
| `killable` | 是否允許在 timeout 時主動 kill 該指令的進程群組。 |

### Python-Side 管線串接

多步驟管線**不使用** Shell 的 `|` 管道符號。取而代之，我們在 Python 端透過 `asyncssh` 的 `stdin=prev.stdout` 參數將前一步的 stdout 直接導入下一步的 stdin：

```python
for i, cmd_args in enumerate(pipeline_cmds):
    p = await conn.create_process(
        command_str,
        stdin=prev_stdout,              # ← Python-side piping
        stdout=asyncssh.PIPE,
        stderr=asyncssh.PIPE
    )
    prev_stdout = p.stdout
```

**優勢**：避免在遠端 Shell 解析 `|`，徹底杜絕透過管道符號注入額外指令的可能性。

---

## 4. Anti-Injection 防注入架構

這是本系統最核心的安全設計。防禦策略分為 **三層縱深**：

### 第一層：字元黑名單（Early Rejection）

在參數進入任何處理流程之前，`_validate_anti_injection()` 會立即掃描是否包含高風險字元：

```python
dangerous_chars = [";", "&", "|", "$", "`"]
if any(char in user_input for char in dangerous_chars):
    raise CommandExecutionException("Invalid characters detected in input.")
```

這一層的目的是**提早拒絕明顯惡意的輸入**，減少後續處理的攻擊面。

### 第二層：Regex 白名單驗證

每個參數在白名單設定中都可以定義 `validation_regex`，例如：
- 數字型參數：`^[0-9]+$`
- 檔案名稱型：`^[a-zA-Z0-9._-]+$`

```python
if arg_conf.validation_regex:
    if not re.match(arg_conf.validation_regex, val_str):
        raise CommandExecutionException("...")
```

### 第三層：shlex 定位引數隔離（Architecture-Level Guarantee）

這是最關鍵也最精巧的一層。即使前面兩層被繞過，這一層在架構上**保證了 Shell Injection 不可能成功**。

#### 為什麼我們需要 `sh -c`？

我們使用以下 wrapper 結構來追蹤 Process Group ID (PGID)：

```python
wrapper = ["setsid", "-w", "sh", "-c", 'echo $$ >&2; exec "$@"', "_"]
full_cmd = wrapper + cmd_args   # e.g., + ["grep", "用戶輸入值"]
command_str = shlex.join(full_cmd)
```

這看起來像是在使用 `sh -c`（通常被認為危險），但實際上**使用者輸入從未進入 `sh -c` 的腳本字串中**。

#### 運作原理解析

假設用戶輸入了惡意值 `$(rm -rf /)`，最終產生的指令會是：

```bash
setsid -w sh -c 'echo $$ >&2; exec "$@"' _ grep '$(rm -rf /)'
```

讓我們逐步分析 Shell 如何解讀這段指令：

| 位置 | Shell 變數 | 實際值 | 說明 |
|---|---|---|---|
| `sh -c` 的腳本 | — | `echo $$ >&2; exec "$@"` | **固定不變**，不含任何用戶輸入 |
| `$0` | `_` | `_` | 慣例的 placeholder |
| `$1` | `"$1"` | `grep` | 白名單定義的執行檔名 |
| `$2` | `"$2"` | `$(rm -rf /)` | **被當作純粹的字串值，不會被 Shell 展開** |

`exec "$@"` 的行為等同於 `exec "$1" "$2" "$3" ...`。因為每個參數都被**雙引號保護**，Shell 不會對它們進行：
- 變數展開 (`$VAR`, `${VAR}`)
- 命令替換 (`$(...)`, `` `...` ``)
- 路徑展開 (`*`, `?`)
- 分詞 (word splitting)

#### 安全性證明

```
用戶輸入: $(rm -rf /)

  ❌ 危險的做法 (字串拼接):
     sh -c "grep $(rm -rf /)"
     → Shell 會先執行 $(rm -rf /) 再執行 grep

  ✅ 我們的做法 (定位引數):
     sh -c 'echo $$ >&2; exec "$@"' _ grep '$(rm -rf /)'
     → Shell 將 '$(rm -rf /)' 視為 $2 的「純值」
     → exec grep '$(rm -rf /)'
     → grep 收到的第一個參數就是字面字串 "$(rm -rf /)"
     → 沒有任何 Shell Expansion 發生
```

`shlex.join()` 額外確保了每個陣列元素在傳輸時被正確的 Shell 引號包裹（使用單引號），防止特殊字元在 SSH 傳輸層被意外解讀。

#### 為什麼不直接拔掉 `sh -c`？

我們保留 `sh -c` 是為了一個不可替代的功能：**擷取 PGID**。

```bash
echo $$ >&2;     # 將當前 Shell 的 PID（即 PGID）寫入 stderr
exec "$@"        # 用 exec 替換自身為目標指令，繼承同一個 PID
```

這個 PGID 是後續 timeout 精準獵殺機制的基礎。如果拿掉 `sh -c`，我們便完全失去追蹤進程群組的能力。

---

## 5. 進程群組追蹤與 Timeout Kill 機制

### 為什麼需要 PGID？

在 SSH 遠端執行指令時，如果直接對 SSH channel 下達中斷，遠端的子進程可能會脫離控制變成孤兒進程 (Orphan Process)。透過追蹤 PGID，我們可以精準找到並終止整個進程樹。

### setsid 隔離策略

```python
wrapper = ["setsid", "-w", "sh", "-c", 'echo $$ >&2; exec "$@"', "_"]
```

| 元件 | 職責 |
|---|---|
| `setsid` | 建立全新的 Session 與 Process Group，確保目標指令不會從屬於 SSH daemon 的 process group |
| `-w` | 等待子進程結束才返回，確保外層可以正確偵測完成狀態 |
| `sh -c '...'` | 在新 session 內啟動 Shell，印出 PGID 後透過 `exec` 無痕替換為目標指令 |
| `echo $$ >&2` | 將 Shell PID（此時即為 Session Leader 的 PGID）輸出到 stderr，供 Python 端讀取 |
| `exec "$@"` | 用目標指令替換 Shell 進程，繼承相同的 PID/PGID |

Python 端從 stderr 讀取這個 PGID：

```python
pgid_str = await p.stderr.readline()
pgids.append(int(pgid_str.strip()))
```

### 兩階段 Kill 策略

當指令超過 `timeout_seconds` 時，系統會啟動兩階段的進程終止策略：

```
Timeout 觸發
    │
    ▼
┌───────────────────┐
│ kill -TERM -{pgid}│  ← 軟殺：發送 SIGTERM，讓進程有機會優雅關閉
└─────────┬─────────┘
          │ 等待 2 秒
          ▼
    ┌──────────┐
    │ 還活著？  │──否──▶ 結束，進程已自行終止
    └────┬─────┘
         │ 是
         ▼
┌───────────────────┐
│ kill -KILL -{pgid}│  ← 硬殺：發送 SIGKILL，強制終止
└───────────────────┘
```

```python
# 軟殺
await conn.run(f"kill -TERM -{pgid}", check=False)
await asyncio.sleep(2)

# 檢查是否還活著
res = await conn.run(f"kill -0 -{pgid}", check=False)
if res.exit_status == 0:
    # 硬殺
    await conn.run(f"kill -KILL -{pgid}", check=False)
```

`kill` 命令前面的負號 `-{pgid}` 代表「向整個 Process Group 發送信號」，確保管線中的所有子進程（如 `sleep`、`grep` 等等）一起被終結。

---

## 6. Fire-and-Forget 斷線指令（如 Reboot）

### 設計思路

某些指令天生會中斷 SSH 連線（例如 `reboot`）。如果用一般的管線執行 + 等待 stdout 的方式處理，我們會永遠收不到結果，因為連線在指令執行後就斷了。

### 雙模式偵測

`_handle_fire_and_forget()` 不會盲目回傳「已發送」，而是根據實際情況做出精準判斷：

```
conn.run(cmd) 執行
    │
    ├── 連線斷開 → Exception 被捕獲
    │   └── 回傳 status: "disconnected_expected" ✅
    │       (這就是正確的 reboot 行為)
    │
    └── 指令正常完成 → 沒有 Exception
        └── 回傳 status: "failed" + 完整 output ⚠️
            (遠端沒有真的斷線，可能是權限不足或環境問題)
```

**真實案例**：Docker 容器通常不支援 `systemd` 的 `reboot`，指令會失敗但不會斷線。在修正前，系統會錯誤地回傳 `disconnected_expected`，讓使用者以為 reboot 成功了。現在系統會正確回傳錯誤訊息：

```json
{
  "status": "failed",
  "message": "Command executed but did not disconnect the session as expected.",
  "exit_status": 1,
  "output": "System has not been booted with systemd as init system (PID 1). Can't operate.\nFailed to connect to bus: Host is down"
}
```

---

## 7. Running Pool 與 Results Pool

### 雙池架構

系統使用兩個獨立的記憶體池來管理指令的生命週期：

```
              API 發起指令
                  │
                  ▼
         ┌─────────────────┐
         │ running_commands │  ← Dict[command_id, RunningCommandEntry]
         │      pool        │     包含 conn, task, processes, pgids
         └────────┬────────┘
                  │ 指令完成 / 逾時 / 失敗
                  ▼
         ┌─────────────────┐
         │ command_results  │  ← Dict[command_id, CommandExecutionResponse]
         │      pool        │     包含 status, output, exit_status
         └─────────────────┘
```

### 輪詢 API

因為長時間執行的指令會在背景 Task 中完成，客戶端需要靠輪詢來取得結果：

```
GET /api/v1/command/execution/{command_id}

→ 若在 running_commands_pool → { "status": "running" }
→ 若在 command_results_pool  → { "status": "success" | "failed", "output": "...", "exit_status": 0 }
→ 若兩者都找不到          → HTTP 404
```

### LRU 快取上限

`command_results_pool` 設有 `MAX_RESULTS_POOL_SIZE = 200` 的上限。每次寫入新結果時，若超出上限則自動刪除最舊的紀錄（FIFO）：

```python
def _prune_results_pool():
    while len(command_results_pool) > MAX_RESULTS_POOL_SIZE:
        oldest_key = next(iter(command_results_pool))
        command_results_pool.pop(oldest_key, None)
```

---

## 8. Graceful Shutdown（優雅關機）

當 FastAPI 應用程式收到中斷信號（如 `Ctrl+C` 或 `SIGTERM`）時，`shutdown_gracefully()` 會被觸發：

```python
async def shutdown_gracefully():
    tasks = [CommandService.kill_command(cmd_id)
             for cmd_id in list(running_commands_pool.keys())]
    if tasks:
        await asyncio.gather(*tasks)
```

此方法會遍歷所有仍在 `running_commands_pool` 中的活躍指令，對每一個執行兩階段 kill，確保遠端主機上不會殘留孤兒進程。

---

## 9. 結構化日誌

所有日誌記錄使用 `extra` 字典攜帶結構化的上下文資訊，支援後續日誌收集系統（如 ELK Stack、Datadog）做 Label 篩選：

```python
logger.info(
    "Initiating command 'sleep' (sleep 180) with timeout 60s.",
    extra={
        "request_id": "e26daab9-30df-...",
        "username": "admin",
        "command_id": "5908e5af-4d3d-..."
    }
)
```

日誌涵蓋的關鍵事件：
- 指令發起（含完整解析後的指令字串）
- PGID 分配
- 指令完成（含 exit status）
- 逾時與進程獵殺
- 軟殺/硬殺每一步的結果
- Fire-and-forget 連線斷開偵測

---

## 10. 附錄：核心檔案索引

| 檔案 | 職責 |
|---|---|
| `app/services/command_service.py` | 核心服務，含 Orchestrator、Pipeline 執行、Kill 邏輯 |
| `app/domain/command.py` | Domain Models：`SSHConnectionConfig`、`RunningCommandEntry`、`ExecutionContext` |
| `app/api/v1/schemas/command.py` | API Schema：Request/Response 模型、Factory Methods |
| `app/api/v1/command.py` | API Routes：`/execution`、`/execution/{id}`、`/info` |
| `app/repositories/ssh_auth_repository.py` | SSH 認證工廠（支援 Key / Certificate） |
| `app/repositories/ssh_key_auth_repository.py` | SSH Key 認證器（Base64 解碼） |
| `app/repositories/ssh_cert_auth_repository.py` | SSH Certificate 認證器（Base64 解碼） |
| `app/core/exceptions.py` | `CommandExecutionException` 繼承自 `BaseAppException` |
| `data/allow-commands-{role}.json` | 角色白名單設定 |
| `data/SSH-{target}.json` | SSH 連線設定（含 Base64 編碼的金鑰） |
