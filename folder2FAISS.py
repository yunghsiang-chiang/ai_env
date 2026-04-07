import os
from pathlib import Path

import faiss
import numpy as np
import pandas as pd
from docx import Document
from sentence_transformers import SentenceTransformer

# ------------------ 設定 ------------------

SOURCE_DIR = "after"
MIN_PARAGRAPH_LEN = 20


# ------------------ 讀取各格式資料 ------------------

def try_open_text_file(filepath: Path) -> str:
    encodings = ["utf-8", "utf-16", "big5"]
    for enc in encodings:
        try:
            with open(filepath, "r", encoding=enc) as f:
                return f.read()
        except Exception:
            continue
    print(f"[WARN] 無法解碼文字檔：{filepath}")
    return ""


def split_into_paragraphs(text: str) -> list[str]:
    """使用雙換行斷段，並過濾過短內容。"""
    return [p.strip() for p in text.split("\n\n") if len(p.strip()) >= MIN_PARAGRAPH_LEN]


def build_qa_paragraph(question: str, answers: list[str], url: str = "") -> str:
    """統一問答資料格式，便於後續向量檢索。"""
    clean_question = question.strip()
    clean_answers = [a.strip() for a in answers if str(a).strip()]
    clean_url = str(url).strip()

    if not clean_question or not clean_answers:
        return ""

    answer_block = "\n".join(f"- {a}" for a in clean_answers)
    chunks = [f"問題：{clean_question}", f"回答：\n{answer_block}"]
    if clean_url:
        chunks.append(f"參考網址：{clean_url}")
    return "\n".join(chunks)


def read_excel_file(filepath: Path) -> list[str]:
    """讀取 Excel（A 問題、B 回答、C 網址）。"""
    paragraphs: list[str] = []
    df = pd.read_excel(filepath, header=None)

    for _, row in df.iterrows():
        question = "" if len(row) < 1 or pd.isna(row.iloc[0]) else str(row.iloc[0]).strip()
        answer = "" if len(row) < 2 or pd.isna(row.iloc[1]) else str(row.iloc[1]).strip()
        url = "" if len(row) < 3 or pd.isna(row.iloc[2]) else str(row.iloc[2]).strip()

        paragraph = build_qa_paragraph(question, [answer], url)
        if paragraph and len(paragraph) >= MIN_PARAGRAPH_LEN:
            paragraphs.append(paragraph)

    return paragraphs


def read_docx_file(filepath: Path) -> list[str]:
    """讀取 Word（Heading 2 為問題、項目符號為回答）。"""
    paragraphs: list[str] = []
    doc = Document(filepath)

    current_question = ""
    current_answers: list[str] = []

    def flush_current_question():
        nonlocal current_answers
        if current_question and current_answers:
            paragraph = build_qa_paragraph(current_question, current_answers)
            if paragraph and len(paragraph) >= MIN_PARAGRAPH_LEN:
                paragraphs.append(paragraph)
        current_answers = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue

        style_name = para.style.name if para.style else ""
        style_name_lower = style_name.lower()

        is_heading_2 = "heading 2" in style_name_lower or "標題 2" in style_name_lower
        is_bullet = "list bullet" in style_name_lower or "項目符號" in style_name_lower

        if is_heading_2:
            flush_current_question()
            current_question = text
            continue

        if is_bullet and current_question:
            current_answers.append(text)

    flush_current_question()
    return paragraphs


def collect_paragraphs(source_dir: str) -> list[str]:
    base = Path(source_dir)
    collected: list[str] = []

    if not base.exists():
        raise FileNotFoundError(f"資料夾不存在：{base.resolve()}")

    for file_path in sorted(base.iterdir()):
        if not file_path.is_file():
            continue

        suffix = file_path.suffix.lower()
        try:
            if suffix == ".txt":
                content = try_open_text_file(file_path)
                if content:
                    collected.extend(split_into_paragraphs(content))
            elif suffix in {".xlsx", ".xls"}:
                collected.extend(read_excel_file(file_path))
            elif suffix == ".docx":
                collected.extend(read_docx_file(file_path))
        except Exception as exc:
            print(f"[WARN] 略過檔案 {file_path.name}，原因：{exc}")

    return collected


# ------------------ 建立向量索引 ------------------

def main():
    paragraphs = collect_paragraphs(SOURCE_DIR)
    print(f"總共取得段落數：{len(paragraphs)}")

    if not paragraphs:
        raise ValueError("沒有可用段落，請確認 after/ 中的 .txt/.xls/.xlsx/.docx 內容。")

    model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    embeddings = model.encode(paragraphs, convert_to_numpy=True)

    index = faiss.IndexFlatL2(embeddings.shape[1])
    index.add(embeddings)

    faiss.write_index(index, "faiss_index.bin")
    np.save("paragraphs.npy", np.array(paragraphs, dtype=object))

    print("已儲存 FAISS 索引與段落內容。")


if __name__ == "__main__":
    main()
