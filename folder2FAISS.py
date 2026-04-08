import os
from pathlib import Path
import json

import faiss
import numpy as np
import pandas as pd
from docx import Document
from sentence_transformers import SentenceTransformer

# ------------------ 設定 ------------------

SOURCE_DIR = "after"
MIN_PARAGRAPH_LEN = 20
SAMPLED_FILES_TRACKER = "sampled_files.json"


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


def load_sampled_files(tracker_path: str) -> set[str]:
    tracker = Path(tracker_path)
    if not tracker.exists():
        return set()
    with open(tracker, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        return {str(item).strip() for item in data if str(item).strip()}
    return set()


def save_sampled_files(tracker_path: str, sampled_files: set[str]) -> None:
    with open(tracker_path, "w", encoding="utf-8") as f:
        json.dump(sorted(sampled_files), f, ensure_ascii=False, indent=2)


def collect_paragraphs(source_dir: str, sampled_files: set[str]) -> tuple[list[str], list[str], set[str]]:
    base = Path(source_dir)
    collected: list[str] = []
    source_files: list[str] = []
    processed_files: set[str] = set()

    if not base.exists():
        raise FileNotFoundError(f"資料夾不存在：{base.resolve()}")

    for file_path in sorted(base.iterdir()):
        if not file_path.is_file():
            continue

        file_name = file_path.name
        if file_name in sampled_files:
            continue

        suffix = file_path.suffix.lower()
        try:
            if suffix == ".txt":
                content = try_open_text_file(file_path)
                if content:
                    file_paragraphs = split_into_paragraphs(content)
                    collected.extend(file_paragraphs)
                    source_files.extend([file_name] * len(file_paragraphs))
                processed_files.add(file_name)
            elif suffix in {".xlsx", ".xls"}:
                file_paragraphs = read_excel_file(file_path)
                collected.extend(file_paragraphs)
                source_files.extend([file_name] * len(file_paragraphs))
                processed_files.add(file_name)
            elif suffix == ".docx":
                file_paragraphs = read_docx_file(file_path)
                collected.extend(file_paragraphs)
                source_files.extend([file_name] * len(file_paragraphs))
                processed_files.add(file_name)
        except Exception as exc:
            print(f"[WARN] 略過檔案 {file_name}，原因：{exc}")

    return collected, source_files, processed_files


# ------------------ 建立向量索引 ------------------

def main():
    sampled_files = load_sampled_files(SAMPLED_FILES_TRACKER)
    new_paragraphs, new_sources, processed_files = collect_paragraphs(SOURCE_DIR, sampled_files)
    print(f"本次新增段落數：{len(new_paragraphs)}")

    existing_paragraphs = np.array([], dtype=object)
    existing_sources = np.array([], dtype=object)
    if Path("paragraphs.npy").exists():
        existing_paragraphs = np.load("paragraphs.npy", allow_pickle=True)
    if Path("paragraph_sources.npy").exists():
        existing_sources = np.load("paragraph_sources.npy", allow_pickle=True)
    if len(existing_sources) != len(existing_paragraphs):
        normalized = np.array(["unknown"] * len(existing_paragraphs), dtype=object)
        copy_len = min(len(existing_sources), len(existing_paragraphs))
        if copy_len > 0:
            normalized[:copy_len] = existing_sources[:copy_len]
        existing_sources = normalized

    if len(new_paragraphs) == 0:
        if processed_files:
            save_sampled_files(SAMPLED_FILES_TRACKER, sampled_files.union(processed_files))
        print("沒有可新增段落，保留原有 paragraphs.npy / faiss_index.bin。")
        return

    paragraphs = np.concatenate([existing_paragraphs, np.array(new_paragraphs, dtype=object)])
    paragraph_sources = np.concatenate([existing_sources, np.array(new_sources, dtype=object)])

    model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
    embeddings = model.encode(paragraphs.tolist(), convert_to_numpy=True)

    index = faiss.IndexFlatL2(embeddings.shape[1])
    index.add(embeddings)

    faiss.write_index(index, "faiss_index.bin")
    np.save("paragraphs.npy", paragraphs)
    np.save("paragraph_sources.npy", paragraph_sources)
    save_sampled_files(SAMPLED_FILES_TRACKER, sampled_files.union(processed_files))

    print("已更新 FAISS 索引、段落內容、來源檔名與取樣檔案清單。")


if __name__ == "__main__":
    main()
