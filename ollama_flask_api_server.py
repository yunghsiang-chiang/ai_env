import time
import threading
import psutil
import os
from functools import wraps
from flask import Flask, request, jsonify
import faiss
import numpy as np
from sentence_transformers import SentenceTransformer
import ollama
import json
import sqlite3


# ------------------ 參數設定 ------------------
MAX_QUESTION_LEN = 300            # 問句長度上限 (字元)
CPU_THRESHOLD = 90.0              # CPU 過載門檻 (%)
MEM_THRESHOLD = 90.0              # RAM 過載門檻 (%)
PORT = 5000                       # Flask 服務埠
TOP_K_DEFAULT = 15                 # 預設檢索段落數
MAX_TOP_K = 30                    # 允許的最大檢索段落數
MODEL_NAME = "llama3"             # Ollama 模型
ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "")
ADMIN_API_KEY_HEADER = "X-Admin-API-Key"
SAMPLED_FILES_TRACKER = "sampled_files.json"


class RWLock:
    """簡易讀寫鎖：允許多讀者，寫入獨佔。"""

    def __init__(self):
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False

    def acquire_read(self):
        with self._cond:
            while self._writer:
                self._cond.wait()
            self._readers += 1

    def release_read(self):
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    def acquire_write(self):
        with self._cond:
            while self._writer or self._readers > 0:
                self._cond.wait()
            self._writer = True

    def release_write(self):
        with self._cond:
            self._writer = False
            self._cond.notify_all()


# ------------------ 全域載入 ------------------
app = Flask(__name__)
rw_lock = RWLock()  # 查詢與管理操作共用同一把讀寫鎖

embedding_model = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')
index = faiss.read_index("faiss_index.bin")
paragraphs = np.load("paragraphs.npy", allow_pickle=True)
if os.path.exists("paragraph_sources.npy"):
    paragraph_sources = np.load("paragraph_sources.npy", allow_pickle=True)
else:
    paragraph_sources = np.array(["unknown"] * len(paragraphs), dtype=object)


# 套用 modifications.json（如有）
def apply_pending_modifications(paragraph_array):
    mod_file = "modifications.json"
    if not os.path.exists(mod_file):
        return paragraph_array, False

    with open(mod_file, "r", encoding="utf-8") as f:
        mods = json.load(f)

    for pid_str, new_text in mods.items():
        pid = int(pid_str)
        if 0 <= pid < len(paragraph_array):
            paragraph_array[pid] = new_text

    os.remove(mod_file)
    return paragraph_array, True


try:
    paragraphs, applied = apply_pending_modifications(paragraphs)
    if applied:
        print("[INFO] Modifications applied from modifications.json")
except Exception as e:
    print("[WARN] Failed to apply modifications.json:", str(e))


# ------------------ 熱載模型 ------------------
print("[INFO] warming up model …")
try:
    ollama.chat(model=MODEL_NAME, messages=[{"role": "user", "content": "ping"}])
except Exception as exc:
    print("[WARN] Warm-up ping failed:", exc)


# ------------------ 工具函式 ------------------
def error_response(code: str, message: str, status: int, details=None):
    payload = {
        "code": code,
        "message": message,
        "details": details,
    }
    return jsonify(payload), status


def normalize_top_k(raw_top_k, total_paragraphs: int) -> int:
    if total_paragraphs <= 0:
        return 0

    try:
        parsed = int(raw_top_k)
    except (TypeError, ValueError):
        parsed = TOP_K_DEFAULT

    if parsed <= 0:
        parsed = TOP_K_DEFAULT

    return max(1, min(parsed, MAX_TOP_K, total_paragraphs))


def compute_embeddings(texts):
    return embedding_model.encode(texts, convert_to_numpy=True)


def ensure_sources_length_match():
    global paragraph_sources
    if len(paragraph_sources) == len(paragraphs):
        return

    normalized = np.array(["unknown"] * len(paragraphs), dtype=object)
    copy_len = min(len(paragraph_sources), len(paragraphs))
    if copy_len > 0:
        normalized[:copy_len] = paragraph_sources[:copy_len]
    paragraph_sources = normalized


ensure_sources_length_match()


def load_sampled_files_tracker() -> list[str]:
    if not os.path.exists(SAMPLED_FILES_TRACKER):
        return []
    try:
        with open(SAMPLED_FILES_TRACKER, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        normalized = sorted({str(item).strip() for item in data if str(item).strip()})
        return normalized
    except Exception:
        return []


def save_sampled_files_tracker(items: list[str]) -> list[str]:
    normalized = sorted({str(item).strip() for item in items if str(item).strip()})
    with open(SAMPLED_FILES_TRACKER, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    return normalized


def retrieve_similar_texts(query, top_k: int = TOP_K_DEFAULT):
    total_paragraphs = len(paragraphs)
    if total_paragraphs == 0:
        return [], 0

    actual_top_k = max(1, min(top_k, MAX_TOP_K, total_paragraphs))
    query_emb = compute_embeddings([query])
    _, idxs = index.search(query_emb, actual_top_k)
    result = []
    for pid in idxs[0]:
        source_file = "unknown"
        if 0 <= pid < len(paragraph_sources):
            source_file = str(paragraph_sources[pid])
        result.append({
            "pid": int(pid),
            "text": str(paragraphs[pid]),
            "source_file": source_file
        })
    return result, actual_top_k


def query_llm(question: str, retrieved_text: str) -> str:
    prompt = (
        "根據以下內容回答問題：\n"
        f"{retrieved_text}\n\n"
        f"問題：{question}\n\n回答："
    )
    resp = ollama.chat(model=MODEL_NAME, messages=[{"role": "user", "content": prompt}])
    return resp["message"]["content"].strip()


def system_overloaded() -> bool:
    return (
        psutil.cpu_percent(interval=0.1) > CPU_THRESHOLD or
        psutil.virtual_memory().percent > MEM_THRESHOLD
    )


def require_admin_api_key(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        if not ADMIN_API_KEY:
            return error_response(
                "ADMIN_API_KEY_NOT_CONFIGURED",
                "伺服器尚未設定管理 API 金鑰",
                500,
                {"required_env": "ADMIN_API_KEY", "header": ADMIN_API_KEY_HEADER},
            )

        incoming_key = request.headers.get(ADMIN_API_KEY_HEADER, "")
        if incoming_key != ADMIN_API_KEY:
            return error_response(
                "ADMIN_UNAUTHORIZED",
                "管理 API 驗證失敗",
                401,
                {"header": ADMIN_API_KEY_HEADER},
            )

        return func(*args, **kwargs)

    return wrapper


# ------------------ 路由 ------------------
@app.route('/admin/source-files', methods=['GET'])
@require_admin_api_key
def list_source_files():
    try:
        files = []
        for f in os.listdir("after"):
            _, ext = os.path.splitext(f)
            if ext.lower() in {".txt", ".xls", ".xlsx", ".docx"}:
                files.append(f)
        sampled = set(load_sampled_files_tracker())
        files.sort()
        return jsonify({
            "folder": "after",
            "file_count": len(files),
            "sampled_count": len(sampled),
            "files": [
                {
                    "file_name": name,
                    "sampled": name in sampled
                }
                for name in files
            ]
        })
    except Exception as e:
        return error_response("INTERNAL_ERROR", "讀取來源檔案失敗", 500, str(e))


@app.route('/admin/sampled-files', methods=['GET'])
@require_admin_api_key
def list_sampled_files():
    rw_lock.acquire_read()
    try:
        sampled = load_sampled_files_tracker()
        return jsonify({
            "tracker_file": SAMPLED_FILES_TRACKER,
            "count": len(sampled),
            "files": sampled,
        })
    except Exception as e:
        return error_response("INTERNAL_ERROR", "讀取已取樣檔案清單失敗", 500, str(e))
    finally:
        rw_lock.release_read()


@app.route('/admin/sampled-files', methods=['POST'])
@require_admin_api_key
def add_sampled_file():
    rw_lock.acquire_write()
    try:
        data = request.get_json(silent=True) or {}
        file_name = str(data.get("file_name", "")).strip()
        if not file_name:
            return error_response("BAD_REQUEST", "請提供 file_name 欄位", 400)

        sampled = load_sampled_files_tracker()
        already_exists = file_name in sampled
        if not already_exists:
            sampled.append(file_name)
            sampled = save_sampled_files_tracker(sampled)

        return jsonify({
            "status": "ok",
            "action": "add",
            "file_name": file_name,
            "already_exists": already_exists,
            "count": len(sampled),
        })
    except Exception as e:
        return error_response("INTERNAL_ERROR", "新增已取樣檔案失敗", 500, str(e))
    finally:
        rw_lock.release_write()


@app.route('/admin/sampled-files/<path:file_name>', methods=['DELETE'])
@require_admin_api_key
def remove_sampled_file(file_name):
    rw_lock.acquire_write()
    try:
        target = str(file_name).strip()
        if not target:
            return error_response("BAD_REQUEST", "請提供檔名", 400)

        sampled = load_sampled_files_tracker()
        if target not in sampled:
            return error_response("NOT_FOUND", "檔案不在已取樣清單中", 404, {"file_name": target})

        sampled = [name for name in sampled if name != target]
        sampled = save_sampled_files_tracker(sampled)
        return jsonify({
            "status": "ok",
            "action": "delete",
            "file_name": target,
            "count": len(sampled),
        })
    except Exception as e:
        return error_response("INTERNAL_ERROR", "刪除已取樣檔案失敗", 500, str(e))
    finally:
        rw_lock.release_write()


@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "cpu": psutil.cpu_percent(interval=0.1),
        "mem": psutil.virtual_memory().percent
    })


@app.route('/paragraphs-by-file', methods=['GET'])
def get_paragraphs_by_file():
    rw_lock.acquire_read()
    try:
        file_name = str(request.args.get("file_name", "")).strip()
        if not file_name:
            return error_response("BAD_REQUEST", "請提供 file_name 參數", 400)

        matched = []
        for i, text in enumerate(paragraphs):
            source_file = str(paragraph_sources[i]) if i < len(paragraph_sources) else "unknown"
            if source_file == file_name:
                matched.append({
                    "pid": i,
                    "text": str(text),
                    "source_file": source_file
                })

        return jsonify({
            "file_name": file_name,
            "count": len(matched),
            "paragraphs": matched
        })

    except Exception as e:
        return error_response("INTERNAL_ERROR", "依檔案查詢段落失敗", 500, str(e))
    finally:
        rw_lock.release_read()


@app.route('/ask', methods=['POST'])
def ask():
    start_ts = time.time()

    if system_overloaded():
        return error_response("SYSTEM_OVERLOADED", "系統負載過高，請稍後重試", 503)

    rw_lock.acquire_read()
    try:
        data = request.get_json(silent=True) or {}
        question = str(data.get("question", "")).strip()
        if not question:
            return error_response("BAD_REQUEST", "請提供 question 參數", 400)
        if len(question) > MAX_QUESTION_LEN:
            return error_response("QUESTION_TOO_LONG", f"問題長度超過 {MAX_QUESTION_LEN} 字元", 400)

        user_top_k = data.get("top_k", TOP_K_DEFAULT)
        actual_top_k = normalize_top_k(user_top_k, len(paragraphs))
        context_items, actual_top_k = retrieve_similar_texts(question, top_k=actual_top_k)
        context = "\n".join(item["text"] for item in context_items).strip()
        answer = query_llm(question, context)

        return jsonify({
            "question": question,
            "context": context,
            "context_items": context_items,
            "answer": answer,
            "top_k": actual_top_k,
            "elapsed_sec": round(time.time() - start_ts, 2)
        })

    except Exception as exc:
        return error_response("INTERNAL_ERROR", "內部錯誤", 500, str(exc))
    finally:
        rw_lock.release_read()


@app.route('/admin/list', methods=['GET'])
@require_admin_api_key
def list_paragraphs():
    rw_lock.acquire_read()
    try:
        page = int(request.args.get('page', 1))
        page_size = int(request.args.get('pageSize', 100))

        if page < 1:
            page = 1
        if page_size < 1:
            page_size = 100

        total = len(paragraphs)
        start = (page - 1) * page_size
        end = min(start + page_size, total)

        if start >= total:
            return jsonify({
                "page": page,
                "pageSize": page_size,
                "total": total,
                "paragraphs": []
            })

        result = {
            "page": page,
            "pageSize": page_size,
            "total": total,
            "paragraphs": [
                {
                    "pid": i,
                    "text": str(paragraphs[i]),
                    "source_file": str(paragraph_sources[i]) if i < len(paragraph_sources) else "unknown",
                }
                for i in range(start, end)
            ]
        }
        return jsonify(result)

    except Exception as e:
        return error_response("INTERNAL_ERROR", "查詢段落失敗", 500, str(e))
    finally:
        rw_lock.release_read()


@app.route('/admin/delete/<int:pid>', methods=['DELETE'])
@require_admin_api_key
def delete_paragraph(pid):
    global paragraphs, paragraph_sources, index

    rw_lock.acquire_write()
    try:
        if pid < 0 or pid >= len(paragraphs):
            return error_response("NOT_FOUND", "段落 ID 不存在", 404, {"pid": pid})

        paragraphs = np.delete(paragraphs, pid)
        paragraph_sources = np.delete(paragraph_sources, pid)
        if len(paragraphs) > 0:
            embeddings = embedding_model.encode(paragraphs.tolist(), convert_to_numpy=True)
            index = faiss.IndexFlatL2(embeddings.shape[1])
            index.add(embeddings)
        else:
            index = faiss.IndexFlatL2(384)

        np.save("paragraphs.npy", paragraphs)
        np.save("paragraph_sources.npy", paragraph_sources)
        faiss.write_index(index, "faiss_index.bin")

        return jsonify({"status": "deleted", "id": pid})

    except Exception as e:
        return error_response("INTERNAL_ERROR", "刪除段落失敗", 500, str(e))
    finally:
        rw_lock.release_write()


@app.route('/admin/update/<int:pid>', methods=['PUT'])
@require_admin_api_key
def update_paragraph(pid):
    rw_lock.acquire_write()
    try:
        data = request.get_json(silent=True) or {}
        new_text = str(data.get("text", "")).strip()
        if not new_text:
            return error_response("BAD_REQUEST", "請提供 text 欄位", 400)

        if pid < 0 or pid >= len(paragraphs):
            return error_response("NOT_FOUND", "段落 ID 不存在", 404, {"pid": pid})

        old_text = str(paragraphs[pid])

        db_path = os.path.join("db", "modification_log.db")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO modification_logs (pid, old_text, new_text)
            VALUES (?, ?, ?)
            """,
            (pid, old_text, new_text),
        )
        conn.commit()
        conn.close()

        mod_file = "modifications.json"
        try:
            if os.path.exists(mod_file):
                with open(mod_file, "r", encoding="utf-8") as f:
                    mods = json.load(f)
            else:
                mods = {}
        except Exception:
            mods = {}

        mods[str(pid)] = new_text
        with open(mod_file, "w", encoding="utf-8") as f:
            json.dump(mods, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "saved", "id": pid, "message": "修改已儲存，重新啟動後生效"})

    except Exception as e:
        return error_response("INTERNAL_ERROR", "更新段落失敗", 500, str(e))
    finally:
        rw_lock.release_write()


@app.route('/admin/pending-modifications', methods=['GET'])
@require_admin_api_key
def get_pending_modifications():
    rw_lock.acquire_read()
    try:
        mod_file = "modifications.json"
        if not os.path.exists(mod_file):
            return jsonify({"pending": {}})

        with open(mod_file, "r", encoding="utf-8") as f:
            mods = json.load(f)
        return jsonify({"pending": mods})

    except Exception as e:
        return error_response("INTERNAL_ERROR", "讀取 pending modifications 失敗", 500, str(e))
    finally:
        rw_lock.release_read()


@app.route('/admin/reload-needed', methods=['GET'])
@require_admin_api_key
def is_reload_needed():
    return jsonify({"reload_needed": os.path.exists("modifications.json")})


@app.route('/admin/reload', methods=['POST'])
@require_admin_api_key
def reload_data():
    global paragraphs, paragraph_sources, index

    rw_lock.acquire_write()
    try:
        paragraphs = np.load("paragraphs.npy", allow_pickle=True)
        if os.path.exists("paragraph_sources.npy"):
            paragraph_sources = np.load("paragraph_sources.npy", allow_pickle=True)
        else:
            paragraph_sources = np.array(["unknown"] * len(paragraphs), dtype=object)
        ensure_sources_length_match()
        paragraphs, applied = apply_pending_modifications(paragraphs)
        if applied:
            print("[INFO] Modifications applied and file removed.")

        if len(paragraphs) > 0:
            embeddings = embedding_model.encode(paragraphs.tolist(), convert_to_numpy=True)
            index = faiss.IndexFlatL2(embeddings.shape[1])
            index.add(embeddings)
        else:
            index = faiss.IndexFlatL2(384)
        print("[INFO] FAISS index rebuilt.")

        return jsonify({"status": "reloaded", "paragraphs_count": len(paragraphs)})

    except Exception as e:
        return error_response("INTERNAL_ERROR", "重新載入資料失敗", 500, str(e))
    finally:
        rw_lock.release_write()


# ------------------ 主程式 ------------------
if __name__ == '__main__':
    print(f"[INFO] Flask API running on 0.0.0.0:{PORT}")
    app.run(host='0.0.0.0', port=PORT, threaded=False)
