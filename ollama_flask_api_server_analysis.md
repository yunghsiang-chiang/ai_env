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
  2. 嘗試取得全域鎖（限制同時間僅一個推論）
  3. 驗證 `question` 與長度
  4. 依 `top_k` 做向量檢索（FAISS）
  5. 將檢索內容拼接後送到 Ollama 回答
- **回傳**：`question`, `context`, `answer`, `elapsed_sec`。
- **適用場景**：內部知識庫問答。

### 3) `GET /admin/source-files`
- **用途**：列出 `after/` 資料夾中的 `.txt` 檔案。
- **回傳**：資料夾名、檔案數、檔案列表。
- **適用場景**：後台查看資料來源是否已放置完整。

### 4) `GET /admin/list`
- **用途**：分頁查詢目前記憶體中的段落資料。
- **參數**：`page`, `pageSize`。
- **回傳**：分頁資訊與段落內容。
- **適用場景**：管理員檢查語料內容、校對段落。

### 5) `DELETE /admin/delete/<pid>`
- **用途**：刪除指定段落並即時重建向量索引。
- **副作用**：
  - 會重算所有 embeddings 並重建 FAISS index。
  - 會覆寫 `paragraphs.npy` 與 `faiss_index.bin`。
- **適用場景**：清除錯誤段落、敏感內容下架。

### 6) `PUT /admin/update/<pid>`
- **用途**：記錄段落修改（延後生效）。
- **副作用**：
  - 將修改寫入 `db/modification_log.db`
  - 將最新文字寫入 `modifications.json`
  - **不會立即更新**記憶體段落與 FAISS index
- **適用場景**：先審核修改，再由管理者統一 reload 生效。

### 7) `GET /admin/pending-modifications`
- **用途**：查詢尚未套用（待生效）的修改集合。
- **回傳**：`modifications.json` 的內容。
- **適用場景**：上線前確認有哪些 pending 變更。

### 8) `GET /admin/reload-needed`
- **用途**：檢查是否有待 reload 的修改。
- **回傳**：`reload_needed: true/false`。
- **適用場景**：後台提示「是否需要重載」。

### 9) `POST /admin/reload`
- **用途**：重新讀取 `paragraphs.npy`，套用 pending 修改，重建 embeddings 與 FAISS index。
- **副作用**：套用後刪除 `modifications.json`。
- **適用場景**：批次生效已登記的內容調整。

---

## 二、整體設計觀察（目前的定位）

這支 API 的定位是「**單機、管理型、可人工維護語料的 RAG 服務**」：
- 對外提供問答 `/ask`
- 對內提供語料管理（list/delete/update/reload）
- 以簡單檔案與 sqlite 做修改紀錄

優點：直觀、容易維運。缺點：在併發、資料一致性、安全性、可觀測性方面還有優化空間。

---

## 三、建議追加功能與修改方向（依優先級）

## P0（建議優先先做）

1. **加入 Admin API 驗證/授權**
   - 現況：`/admin/*` 無驗證。
   - 風險：任意人可刪除、修改、重建索引。
   - 建議：最少先加 API Key（Header），再逐步升級 JWT / OAuth。

2. **資料操作加鎖，避免併發競態**
   - 現況：`/ask` 有 lock，但 `/admin/delete`、`/admin/reload`、`/admin/update` 無共用鎖策略。
   - 風險：推論同時遇到索引重建，可能出現不一致或偶發錯誤。
   - 建議：用同一把讀寫鎖（RWLock）管理查詢與寫入。

3. **修正 `top_k` 預設邏輯**
   - 現況：`/ask` 解析失敗時預設用 `MAX_TOP_K`，不是 `TOP_K_DEFAULT`。
   - 影響：預設檢索量偏大，增加延遲與噪音。
   - 建議：改為 `TOP_K_DEFAULT`，並在 response 回傳實際使用的 `top_k`。

4. **明確錯誤碼與錯誤格式標準化**
   - 現況：錯誤訊息格式不完全一致。
   - 建議：統一 `{code, message, details}`，便於前端與監控系統處理。

## P1（中期）

5. **新增 `POST /admin/reindex` 與背景工作機制**
   - 現況：刪除或 reload 都同步重建索引，可能阻塞。
   - 建議：改成背景 job（例如 Celery/RQ 或執行緒任務）+ 任務狀態查詢 API。

6. **加入觀測性（metrics + 結構化 log）**
   - 建議指標：QPS、p95 延遲、LLM 呼叫耗時、檢索耗時、錯誤率、拒絕率（503）。
   - 可先導入 Prometheus 指標端點與 request id。

7. **上下文品質控制**
   - 現況：直接把 top-k 全文串接。
   - 建議：
     - 加總 token 上限（context budget）
     - 段落去重、截斷、排序策略
     - 可選 reranker（提升答案品質）

8. **新增 `POST /ask/stream`（串流回覆）**
   - 好處：降低使用者等待體感，提高產品可用性。

## P2（長期）

9. **版本化與審核流程**
   - 建議：
     - `modification_logs` 增加 reviewer、status、timestamp
     - 支援 approve/reject
     - 支援 rollback 到指定版本

10. **多模型/多索引配置化**
   - 現況：模型與索引路徑寫死。
   - 建議：支援環境變數與租戶化設定（如 model per project）。

11. **拆分 Blueprint 與 service layer**
   - 目前單檔邏輯集中，後續維護成本會上升。
   - 建議切分：`routes/`, `services/`, `repositories/`, `schemas/`。

---

## 四、快速可落地的小修清單（低成本高回報）

- [ ] 移除重複的 `import os`。
- [ ] `POST /ask` 的 `user_top_k` fallback 改為 `TOP_K_DEFAULT`。
- [ ] `/admin/*` 最少加 API Key 驗證。
- [ ] 為 `/admin/delete`、`/admin/reload` 加入鎖。
- [ ] `update` 端點中 `new_text` 指派邏輯可簡化，避免重複覆蓋。
- [ ] 補上 OpenAPI/Swagger 文件，讓前端可直接對接。

---

## 五、結論

目前 API 已具備「可運作」的 RAG + 後台管理基礎能力，適合作為內部工具第一版。若要進入多人使用或半正式上線，優先補齊 **安全性（Admin auth）**、**一致性（鎖與重建策略）**、**可觀測性（metrics/log）** 三件事，風險會明顯下降。
