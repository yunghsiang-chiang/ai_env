# `ollama_flask_api_server.py` API 方法對應用意與建議

## 一、目前 API 方法與用途對照

### 1) `GET /health`
- **用途**：健康檢查與基本系統監控。
- **回傳**：`status`, `cpu`, `mem`。
- **適用場景**：容器監控、負載平衡器健康探測、上線後巡檢。

### 2) `POST /ask`
- **用途**：主要問答入口（RAG + LLM）。
- **流程**：
  1. 檢查系統負載（CPU/MEM 門檻）
  2. 取得 **RWLock 讀鎖**（允許讀、阻擋寫）
  3. 驗證 `question` 與長度
  4. 解析 `top_k`（失敗時改用 `TOP_K_DEFAULT`）
  5. 依 `top_k` 做向量檢索（FAISS）
  6. 將檢索內容拼接後送到 Ollama 回答
- **回傳**：`question`, `context`, `answer`, `top_k`, `elapsed_sec`。
- **適用場景**：內部知識庫問答。

### 3) `GET /admin/source-files`
- **用途**：列出 `after/` 資料夾中的 `.txt` 檔案。
- **安全**：需 `X-Admin-API-Key`。
- **回傳**：資料夾名、檔案數、檔案列表。

### 4) `GET /admin/list`
- **用途**：分頁查詢目前記憶體中的段落資料。
- **安全**：需 `X-Admin-API-Key`。
- **鎖策略**：RWLock 讀鎖。

### 5) `DELETE /admin/delete/<pid>`
- **用途**：刪除指定段落並即時重建向量索引。
- **安全**：需 `X-Admin-API-Key`。
- **鎖策略**：RWLock 寫鎖（獨佔）。

### 6) `PUT /admin/update/<pid>`
- **用途**：記錄段落修改（延後生效）。
- **安全**：需 `X-Admin-API-Key`。
- **鎖策略**：RWLock 寫鎖（獨佔）。

### 7) `GET /admin/pending-modifications`
- **用途**：查詢尚未套用（待生效）的修改集合。
- **安全**：需 `X-Admin-API-Key`。

### 8) `GET /admin/reload-needed`
- **用途**：檢查是否有待 reload 的修改。
- **安全**：需 `X-Admin-API-Key`。

### 9) `POST /admin/reload`
- **用途**：重新讀取 `paragraphs.npy`，套用 pending 修改，重建 embeddings 與 FAISS index。
- **安全**：需 `X-Admin-API-Key`。
- **鎖策略**：RWLock 寫鎖（獨佔）。

---

## 二、本次已完成項目（2026-04-07）

### ✅ P0 需求完成狀態

1. **Admin API 驗證/授權**
   - 已新增 `require_admin_api_key` decorator。
   - 所有 `/admin/*` 端點都要求 Header：`X-Admin-API-Key`。
   - 金鑰從環境變數 `ADMIN_API_KEY` 載入。

2. **資料操作加鎖，避免併發競態**
   - 已導入共用 `RWLock`。
   - `/ask` 使用 **讀鎖**。
   - `/admin/delete`、`/admin/update`、`/admin/reload` 使用 **寫鎖**。
   - `/admin/list`、`/admin/pending-modifications` 使用 **讀鎖**。

3. **修正 `top_k` 預設邏輯**
   - `top_k` 解析失敗或非法值時，改為 `TOP_K_DEFAULT`。
   - `/ask` response 新增 `top_k` 欄位，回傳實際使用值。

4. **錯誤碼與錯誤格式標準化**
   - 新增統一錯誤回應 helper：`error_response`。
   - 錯誤格式統一為：`{code, message, details}`。

---

## 三、快速可落地小修清單（更新後）

- [x] 移除重複的 `import os`。
- [x] `POST /ask` 的 `user_top_k` fallback 改為 `TOP_K_DEFAULT`。
- [x] `/admin/*` 加入 API Key 驗證。
- [x] 為 `/admin/delete`、`/admin/reload`、`/admin/update` 加入共用鎖策略。
- [x] `update` 端點中 `new_text` 指派邏輯簡化。
- [ ] 補上 OpenAPI/Swagger 文件，讓前端可直接對接。

---

## 四、部署設定提醒

請在啟動服務前設定：

```bash
export ADMIN_API_KEY="your-strong-key"
```

管理端呼叫需帶上：

```http
X-Admin-API-Key: your-strong-key
```

若未設定 `ADMIN_API_KEY`，管理端點會回傳：
- `500 ADMIN_API_KEY_NOT_CONFIGURED`

若金鑰錯誤：
- `401 ADMIN_UNAUTHORIZED`

## 五、常見錯誤：`ADMIN_API_KEY_NOT_CONFIGURED` 排查與設定

若前端看到：

- `錯誤：無法取得段落資料（HTTP 500 / ADMIN_API_KEY_NOT_CONFIGURED）：伺服器尚未設定管理 API 金鑰`

這是**後端 API 服務設定問題**（不是前端參數格式問題）。

### 原因

`/admin/*` 端點在進入業務邏輯前，會先檢查後端程序的環境變數 `ADMIN_API_KEY`。

- 沒有設定：回傳 `500 ADMIN_API_KEY_NOT_CONFIGURED`
- 有設定但請求 Header 不符：回傳 `401 ADMIN_UNAUTHORIZED`

### 設定方式（Linux/macOS）

#### 1) 先在啟動同一個 shell 設定環境變數

```bash
export ADMIN_API_KEY='replace-with-a-strong-secret'
python ollama_flask_api_server.py
```

#### 2) 呼叫管理端點時帶上 Header

```bash
curl -H "X-Admin-API-Key: $ADMIN_API_KEY" http://127.0.0.1:5000/admin/list
```

### 如果你用 systemd 啟動

在 service 檔加入：

```ini
Environment="ADMIN_API_KEY=replace-with-a-strong-secret"
```

變更後執行：

```bash
sudo systemctl daemon-reload
sudo systemctl restart <your-service-name>
```

### 如果你用 Docker 啟動

```bash
docker run -e ADMIN_API_KEY='replace-with-a-strong-secret' -p 5000:5000 <image>
```

### 最快自我檢查

```bash
echo "$ADMIN_API_KEY"
```

- 空值表示目前 shell/程序沒有拿到 key。
- 注意：若你是在 A terminal 設定 `export`，卻在 B terminal 啟動服務，B terminal 不會自動有該變數。
