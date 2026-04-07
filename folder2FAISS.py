import os
import numpy as np
import faiss
from sentence_transformers import SentenceTransformer

# ------------------ 載入文字資料 ------------------

TEXT_DIR = "after"
all_text = ""

def try_open_file(filepath):
    encodings = ["utf-8", "utf-16", "big5"]
    for enc in encodings:
        try:
            with open(filepath, "r", encoding=enc) as f:
                return f.read()
        except Exception:
            continue
    print(f"[WARN] 無法解碼檔案：{filepath}")
    return ""

for filename in os.listdir(TEXT_DIR):
    if filename.endswith(".txt"):
        file_path = os.path.join(TEXT_DIR, filename)
        content = try_open_file(file_path)
        if content:
            all_text += content + "\n\n"  # 保留自然段落的空行

# ------------------ 改用自然段落分割 ------------------

def split_into_paragraphs(text):
    """ 使用雙換行斷段，並過濾過短內容 """
    paragraphs = text.split("\n\n")
    paragraphs = [p.strip() for p in paragraphs if len(p.strip()) > 20]
    return paragraphs

paragraphs = split_into_paragraphs(all_text)
print(f"總共取得段落數：{len(paragraphs)}")

# ------------------ 建立向量索引 ------------------

model = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')
embeddings = model.encode(paragraphs, convert_to_numpy=True)

index = faiss.IndexFlatL2(embeddings.shape[1])
index.add(embeddings)

# ------------------ 儲存索引與段落 ------------------

faiss.write_index(index, "faiss_index.bin")
np.save("paragraphs.npy", np.array(paragraphs))

print("已儲存 FAISS 索引與段落內容。")
