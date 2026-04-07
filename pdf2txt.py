import fitz  # PyMuPDF
import os

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer
import ollama


# 設定 PDF 檔案路徑
pdf_path = "幸福印記入口解壓靈.txt"

def extract_text_from_pdf(pdf_path):
    """ 解析 PDF 內容，轉換為純文本 """
    doc = fitz.open(pdf_path)
    text = ""
    for page in doc:
        text += page.get_text("text") + "\n\n"
    return text

# 解析 PDF 並儲存為純文本
pdf_text = extract_text_from_pdf(pdf_path)

# 顯示部分解析結果
# print(pdf_text[:1000])  # 只顯示前 1000 字，確認解析是否成功

def split_text_into_paragraphs(text):
    """ 以段落為單位分割文本 """
    paragraphs = text.split("\n\n")  # 根據兩個換行符切割
    paragraphs = [p.strip() for p in paragraphs if len(p.strip()) > 20]  # 過濾過短的段落
    return paragraphs

def query_llm(question, retrieved_text):
    """ 使用本地 GPT (Ollama) 生成答案 """
    prompt = f"根據以下內容回答問題：\n{retrieved_text}\n\n問題：{question}\n\n回答："
    response = ollama.chat(model="llama3", messages=[{"role": "user", "content": prompt}])
    return response["message"]["content"]

# 斷句後的文本
paragraphs = split_text_into_paragraphs(pdf_text)

# 顯示部分分割結果
# print("\n".join(paragraphs[:5]))  # 顯示前 5 段，確認切割結果




# 初始化 Sentence-Transformers 模型
embedding_model = SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')

# 計算嵌入
def compute_embeddings(texts):
    """ 將文本轉換為嵌入向量 """
    return embedding_model.encode(texts, convert_to_numpy=True)

# 計算段落嵌入
paragraph_embeddings = compute_embeddings(paragraphs)

# 建立 FAISS 索引
dimension = paragraph_embeddings.shape[1]
index = faiss.IndexFlatL2(dimension)
index.add(paragraph_embeddings)

# 儲存索引
faiss.write_index(index, "faiss_index.bin")
np.save("paragraphs.npy", paragraphs)  # 儲存段落內容


# 讀取 FAISS 索引與段落內容
index = faiss.read_index("faiss_index.bin")
paragraphs = np.load("paragraphs.npy", allow_pickle=True)

def retrieve_similar_texts(query, top_k=3):
    """ 在向量資料庫中查詢最相關的段落 """
    query_embedding = compute_embeddings([query])
    distances, indices = index.search(query_embedding, top_k)
    return [paragraphs[i] for i in indices[0]]

# # 測試檢索
# query = "幸福印記的核心概念是什麼？"
# retrieved_texts = retrieve_similar_texts(query)
# print("\n".join(retrieved_texts))

# print("----------------------------------------------------")
# # 測試檢索
# query = "什麼是壓靈？"
# retrieved_texts = retrieve_similar_texts(query)
# print("\n".join(retrieved_texts))

# 測試 GPT 回答
query = "幸福印記的核心概念是什麼？"
retrieved_context = "\n".join(retrieve_similar_texts(query))
answer = query_llm(query, retrieved_context)
print("GPT 回答：", answer)
