import time
import threading
import psutil
import os
from flask import Flask, request, jsonify
import faiss
import numpy as np
from sentence_transformers import SentenceTransformer
import ollama
import json
import sqlite3
import os


# ------------------ 參數設定 ------------------
MAX_QUESTION_LEN = 300           # 問句長度上限 (字元)
CPU_THRESHOLD    = 90.0          # CPU 過載門檻 (%)
MEM_THRESHOLD    = 90.0          # RAM 過載門檻 (%)
PORT             = 5000          # Flask 服務埠
TOP_K_DEFAULT    = 5             # 預設檢索段落數
MAX_TOP_K        = 10             # 允許的最大檢索段落數
MODEL_NAME       = "llama3"      # Ollama 模型

# ------------------ 全域載入 ------------------
app = Flask(__name__)
lock = threading.Lock()           # 單一推論鎖

embedding_model = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')
index      = faiss.read_index("faiss_index.bin")
paragraphs = np.load("paragraphs.npy", allow_pickle=True)

# 套用 modifications.json（如有）
mod_file = "modifications.json"
if os.path.exists(mod_file):
    try:
        with open(mod_file, "r", encoding="utf-8") as f:
            mods = json.load(f)
        for pid_str, new_text in mods.items():
            pid = int(pid_str)
            if 0 <= pid < len(paragraphs):
                paragraphs[pid] = new_text
        # 套用後刪除 modifications.json
        os.remove(mod_file)
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
def compute_embeddings(texts):
    return embedding_model.encode(texts, convert_to_numpy=True)

def retrieve_similar_texts(query, top_k: int = TOP_K_DEFAULT):
    total_paragraphs = len(paragraphs)
    if total_paragraphs == 0:
        return []

    top_k = max(1, min(top_k, MAX_TOP_K, total_paragraphs))
    query_emb = compute_embeddings([query])
    _, idxs = index.search(query_emb, top_k)
    return [paragraphs[i] for i in idxs[0]]

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

# ------------------ 路由 ------------------
@app.route('/admin/source-files', methods=['GET'])
def list_source_files():
    try:
        files = [f for f in os.listdir("after") if f.endswith(".txt")]
        return jsonify({
            "folder": "after",
            "file_count": len(files),
            "files": files
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "cpu": psutil.cpu_percent(interval=0.1),
        "mem": psutil.virtual_memory().percent
    })

@app.route('/ask', methods=['POST'])
def ask():
    data = request.get_json()
    print("[DEBUG] data from POST /ask:", data)
    
    start_ts = time.time()

    if system_overloaded():
        return jsonify({"error": "系統負載過高，請稍後重試"}), 503

    if not lock.acquire(blocking=False):
        return jsonify({"error": "伺服器忙碌中，請稍後重試"}), 503

    try:
        data = request.get_json(silent=True) or {}
        question = str(data.get("question", "")).strip()
        if not question:
            return jsonify({"error": "請提供 question 參數"}), 400
        if len(question) > MAX_QUESTION_LEN:
            return jsonify({"error": f"問題長度超過 {MAX_QUESTION_LEN} 字元"}), 400

        try:
            user_top_k = int(data.get("top_k", MAX_TOP_K))
        except (TypeError, ValueError):
            user_top_k = MAX_TOP_K

        context = "\n".join(retrieve_similar_texts(question, top_k=user_top_k)).strip()
        answer  = query_llm(question, context)

        return jsonify({
            "question": question,
            "context": context,
            "answer": answer,
            "elapsed_sec": round(time.time() - start_ts, 2)
        })

    except Exception as exc:
        return jsonify({"error": f"內部錯誤：{exc}"}), 500

    finally:
        lock.release()

@app.route('/admin/list', methods=['GET'])
def list_paragraphs():
    try:
        # 預設值
        page = int(request.args.get('page', 1))
        page_size = int(request.args.get('pageSize', 100))

        # 邊界控制
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
            "paragraphs": {
                i: paragraphs[i] for i in range(start, end)
            }
        }

        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/admin/delete/<int:pid>', methods=['DELETE'])
def delete_paragraph(pid):
    global paragraphs, index
    try:
        if pid < 0 or pid >= len(paragraphs):
            return jsonify({"error": "段落 ID 不存在"}), 404

        paragraphs = np.delete(paragraphs, pid)
        embeddings = embedding_model.encode(paragraphs.tolist(), convert_to_numpy=True)
        index = faiss.IndexFlatL2(embeddings.shape[1])
        index.add(embeddings)

        np.save("paragraphs.npy", paragraphs)
        faiss.write_index(index, "faiss_index.bin")

        return jsonify({"status": "deleted", "id": pid})

    except Exception as e:
        return jsonify({"error": str(e)}), 500
    
# 1. 修改段落內容，寫入修改紀錄檔
@app.route('/admin/update/<int:pid>', methods=['PUT'])
def update_paragraph(pid):
    try:
        data = request.get_json()
        new_text = str(data.get("text", "")).strip()
        if not new_text:
            return jsonify({"error": "請提供 text 欄位"}), 400

        if pid < 0 or pid >= len(paragraphs):
            return jsonify({"error": "段落 ID 不存在"}), 404

        # 備份舊內容
        old_text = str(paragraphs[pid])
        new_text = data.get("text", "").strip()
        
        db_path = os.path.join("db", "modification_log.db")
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO modification_logs (pid, old_text, new_text)
            VALUES (?, ?, ?)
        """, (pid, old_text, new_text))
        conn.commit()
        conn.close()

        # 寫入修改紀錄
        mod_file = "modifications.json"
        try:
            if os.path.exists(mod_file):
                with open(mod_file, "r", encoding="utf-8") as f:
                    mods = json.load(f)
            else:
                mods = {}
        except:
            mods = {}

        mods[str(pid)] = new_text
        with open(mod_file, "w", encoding="utf-8") as f:
            json.dump(mods, f, ensure_ascii=False, indent=2)

        return jsonify({"status": "saved", "id": pid, "message": "修改已儲存，重新啟動後生效"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# 2. 查詢等待修改的段落（modifications.json）
@app.route('/admin/pending-modifications', methods=['GET'])
def get_pending_modifications():
    try:
        mod_file = "modifications.json"
        if not os.path.exists(mod_file):
            return jsonify({"pending": {}})

        with open(mod_file, "r", encoding="utf-8") as f:
            mods = json.load(f)
        return jsonify({"pending": mods})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# 3. 是否有等待更新的資料
@app.route('/admin/reload-needed', methods=['GET'])
def is_reload_needed():
    return jsonify({"reload_needed": os.path.exists("modifications.json")})

# 重新載入 paragraphs.npy、重新套用 modifications.json（如存在）、重新建構 embedding 並更新 FAISS index、移除 modifications.json，避免重複套用
@app.route('/admin/reload', methods=['POST'])
def reload_data():
    global paragraphs, index

    try:
        # 1. 重新讀取原始資料
        paragraphs = np.load("paragraphs.npy", allow_pickle=True)

        # 2. 套用修改
        mod_file = "modifications.json"
        if os.path.exists(mod_file):
            with open(mod_file, "r", encoding="utf-8") as f:
                mods = json.load(f)
            for pid_str, new_text in mods.items():
                pid = int(pid_str)
                if 0 <= pid < len(paragraphs):
                    paragraphs[pid] = new_text
            os.remove(mod_file)
            print("[INFO] Modifications applied and file removed.")

        # 3. 重新計算 embeddings 並建立 FAISS index
        embeddings = embedding_model.encode(paragraphs.tolist(), convert_to_numpy=True)
        index = faiss.IndexFlatL2(embeddings.shape[1])
        index.add(embeddings)
        print("[INFO] FAISS index rebuilt.")

        return jsonify({"status": "reloaded", "paragraphs_count": len(paragraphs)})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ------------------ 主程式 ------------------
if __name__ == '__main__':
    print(f"[INFO] Flask API running on 0.0.0.0:{PORT}")
    app.run(host='0.0.0.0', port=PORT, threaded=False)
