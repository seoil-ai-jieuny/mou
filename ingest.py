"""
ingest.py
─────────────────────────────────────────────────────
규정 QA 지식 인덱싱 스크립트 (Gemini 임베딩 사용).

★ 변경: 기존 index.html의 #partners(MOU 파트너 카드) 데이터는 더 이상 사용하지 않습니다.
  이제 ./knowledge/ 폴더의 "규정 QA 세트(산학협력·MOU)" JSONL만을 단일 지식 출처로 인덱싱합니다.
  (추가로 ./docs/ 폴더의 .md/.txt/.pdf 보조 문서가 있으면 함께 인덱싱)

태스크 타입:
- RETRIEVAL_DOCUMENT  → 인덱싱 시 문서 임베딩 (여기서 사용)
- RETRIEVAL_QUERY     → 검색 시 질문 임베딩 (server.py에서 사용)

사용:
    python ingest.py
    python ingest.py --reset   # 기존 인덱스 삭제 후 재구축 (지식 교체 시 권장)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import re
import time
from pathlib import Path

from dotenv import load_dotenv, find_dotenv
from google import genai
from google.genai import types
import chromadb


# ─── 설정 로드 ─────────────────────────────────────
load_dotenv(find_dotenv(usecwd=True))

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
EMBED_MODEL = os.getenv("EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))
CHROMA_DIR = os.getenv("CHROMA_DIR", "./chroma_db")
# 단일 지식 출처: 규정 QA 세트(JSONL) 폴더
KNOWLEDGE_DIR = os.getenv("KNOWLEDGE_DIR", "./knowledge")
# (선택) 보조 문서 폴더
DOCS_DIR = os.getenv("DOCS_DIR", "./docs")
COLLECTION_NAME = "mou_knowledge"

# ── Gemini 무료 티어 보호 설정 ──────────────────────
# 무료 티어 임베딩은 분당 요청수(RPM)가 매우 낮습니다(대략 5~15 RPM, 2025.12 인하).
# 한 번에 너무 많이/빠르게 보내면 429(rate limit)로 인덱싱이 통째로 실패합니다.
# → 배치를 작게, 배치 간 대기를 충분히 둡니다. 환경변수로 조정 가능.
EMBED_BATCH_SIZE = int(os.getenv("EMBED_BATCH_SIZE", "5"))
SLEEP_BETWEEN_BATCHES = float(os.getenv("SLEEP_BETWEEN_BATCHES", "5.0"))


# ─── 유틸 ──────────────────────────────────────────
def log(msg: str) -> None:
    print(f"[ingest] {msg}", flush=True)


def clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ─── 규정 QA 지식(JSONL) 로드 ──────────────────────
def load_qa_knowledge(knowledge_dir: Path) -> list[dict]:
    """./knowledge/*.jsonl 의 각 QA 레코드를 1개의 검색 문서로 변환.

    기대 JSONL 스키마(한 줄당 하나):
      {"id": "...", "text": "...", "metadata": {...}}
    또는 원시 QA 스키마:
      {"id","question","ground_truth_answer","source":{...},"evidence",...}
    둘 다 자동 처리합니다.
    """
    if not knowledge_dir.exists():
        log(f"✗ knowledge 폴더가 없습니다: {knowledge_dir}")
        return []

    files = sorted(knowledge_dir.glob("*.jsonl"))
    if not files:
        log(f"✗ {knowledge_dir} 안에 .jsonl 지식 파일이 없습니다.")
        return []

    out: list[dict] = []
    for f in files:
        n = 0
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            # 이미 text/metadata 형태면 그대로 사용
            if "text" in rec:
                doc_id = str(rec.get("id") or f"{f.stem}-{n}")
                text = rec["text"]
                meta = rec.get("metadata", {}) or {}
            else:
                # 원시 QA 스키마 → text 조립
                q = rec.get("question", "")
                a = rec.get("ground_truth_answer") or rec.get("answer", "")
                src = rec.get("source", {}) or {}
                reg = src.get("regulation", "")
                art = src.get("article", "")
                ev = rec.get("evidence", "")
                text = (f"[질문] {q}\n[답변] {a}\n"
                        f"[근거 규정] {reg} {art}\n[근거 원문] {ev}")
                doc_id = str(rec.get("id") or f"{f.stem}-{n}")
                meta = {
                    "title": f"{reg} {art}".strip(),
                    "regulation": reg,
                    "article": art,
                    "category": rec.get("category", ""),
                    "question_type": rec.get("question_type", ""),
                    "source_path": src.get("path", ""),
                }

            # Chroma 메타데이터는 스칼라만 허용 → 정리
            meta = {k: ("" if v is None else v) for k, v in meta.items()
                    if isinstance(v, (str, int, float, bool)) or v is None}
            meta.setdefault("source", str(f.name))
            meta.setdefault("type", "regulation-qa")

            out.append({"id": doc_id, "text": text, "metadata": meta})
            n += 1
        log(f"  {f.name} → {n} QA 문서")
    log(f"규정 QA 지식 문서 총: {len(out)}개")
    return out


# ─── (선택) 보조 문서 (docs/) ──────────────────────
def read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        log(f"⚠ pypdf 미설치 — PDF 건너뜀: {path}")
        return ""
    try:
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as e:
        log(f"⚠ PDF 읽기 실패 ({path}): {e}")
        return ""


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 120) -> list[str]:
    text = text.strip()
    if not text:
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for p in paragraphs:
        if len(buf) + len(p) + 2 <= chunk_size:
            buf = f"{buf}\n\n{p}" if buf else p
        else:
            if buf:
                chunks.append(buf)
            if len(p) <= chunk_size:
                buf = p
            else:
                start = 0
                while start < len(p):
                    chunks.append(p[start: start + chunk_size])
                    start += chunk_size - overlap
                buf = ""
    if buf:
        chunks.append(buf)
    return chunks


def load_docs_folder(docs_dir: Path) -> list[dict]:
    if not docs_dir.exists():
        return []
    out: list[dict] = []
    files = sorted(
        [f for f in docs_dir.rglob("*") if f.is_file() and f.suffix.lower() in {".md", ".txt", ".pdf"}]
    )
    for f in files:
        suffix = f.suffix.lower()
        try:
            text = read_pdf(f) if suffix == ".pdf" else f.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            log(f"⚠ 파일 읽기 실패 ({f}): {e}")
            continue
        chunks = chunk_text(text)
        rel = f.relative_to(docs_dir)
        if chunks:
            log(f"  (보조) {rel} → {len(chunks)} chunks")
        for i, ch in enumerate(chunks):
            out.append({
                "id": f"doc-{re.sub(r'[^a-zA-Z0-9가-힣]+', '_', str(rel))}-{i}",
                "text": ch,
                "metadata": {"source": str(rel), "type": "external-doc",
                             "title": f.stem, "chunk_index": i},
            })
    return out


# ─── Gemini 임베딩 ─────────────────────────────────
def _extract_retry_delay(err_text: str) -> float | None:
    """429 오류 메시지에서 권장 대기시간(retryDelay: '17s')을 파싱."""
    m = re.search(r"retry[_ ]?delay['\":\s]+(\d+)\s*s", err_text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d+)\s*s\s*대기", err_text)
    if m:
        return float(m.group(1))
    return None


def embed_batch(client: genai.Client, texts: list[str], max_attempts: int = 8) -> list[list[float]]:
    """임베딩 호출. 429(rate limit) 시 API가 알려주는 대기시간을 존중하며 길게 재시도."""
    last = ""
    for attempt in range(max_attempts):
        try:
            resp = client.models.embed_content(
                model=EMBED_MODEL,
                contents=texts,
                config=types.EmbedContentConfig(
                    task_type="RETRIEVAL_DOCUMENT",
                    output_dimensionality=EMBED_DIM,
                ),
            )
            return [e.values for e in resp.embeddings]
        except Exception as e:
            last = str(e)
            is_rate = ("429" in last) or ("RESOURCE_EXHAUSTED" in last) or ("rate" in last.lower())
            if is_rate:
                suggested = _extract_retry_delay(last)
                wait = (suggested + 3) if suggested else min(60, 15 * (attempt + 1))
                log(f"  ⏳ rate limit(429) — {wait:.0f}s 대기 후 재시도 ({attempt + 1}/{max_attempts})")
            else:
                wait = 2 ** attempt + 1
                log(f"  임베딩 재시도 {attempt + 1}/{max_attempts} ({last[:120]}) — {wait}s 대기")
            time.sleep(wait)
    raise RuntimeError(
        f"임베딩 {max_attempts}회 실패. 무료 티어 RPM 한도이거나 API 키/할당량 문제일 수 있습니다. "
        f"EMBED_BATCH_SIZE를 더 줄이거나 SLEEP_BETWEEN_BATCHES를 늘려보세요."
    )


# ─── 메인 ──────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="기존 컬렉션 삭제 후 재구축")
    args = parser.parse_args()

    if not GEMINI_API_KEY:
        log("✗ GEMINI_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.")
        log("  발급: https://aistudio.google.com/apikey")
        sys.exit(1)

    log(f"임베딩 모델: {EMBED_MODEL} (dim={EMBED_DIM})")
    log(f"Chroma 디렉토리: {CHROMA_DIR}")
    log("지식 출처: 규정 QA 세트(산학협력·MOU) — index.html 파트너 데이터는 사용하지 않음")

    # 1. 문서 수집 (규정 QA 세트가 단일 출처)
    docs = []
    docs.extend(load_qa_knowledge(Path(KNOWLEDGE_DIR).resolve()))
    docs.extend(load_docs_folder(Path(DOCS_DIR).resolve()))  # 보조 문서(선택)

    if not docs:
        log("✗ 인덱싱할 문서가 없습니다. ./knowledge/*.jsonl 을 확인하세요.")
        sys.exit(1)

    log(f"총 문서 수: {len(docs)}")

    # 2. Chroma 컬렉션 준비
    chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)
    if args.reset:
        try:
            chroma_client.delete_collection(COLLECTION_NAME)
            log("기존 컬렉션 삭제 완료 (지식 교체)")
        except Exception:
            pass
    collection = chroma_client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )

    # 3. 이미 인덱싱된 id 건너뛰기
    existing = set()
    try:
        existing = set(collection.get(include=[])["ids"])
    except Exception:
        pass
    new_docs = [d for d in docs if d["id"] not in existing]
    log(f"신규 인덱싱 대상: {len(new_docs)}개 (기존 {len(existing)}개 유지)")

    if not new_docs:
        log("✓ 모든 문서가 이미 인덱싱되어 있습니다.")
        return

    # 4. Gemini로 임베딩 + Chroma 업로드 (배치)
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    total_batches = (len(new_docs) + EMBED_BATCH_SIZE - 1) // EMBED_BATCH_SIZE
    for i in range(0, len(new_docs), EMBED_BATCH_SIZE):
        batch = new_docs[i: i + EMBED_BATCH_SIZE]
        batch_idx = i // EMBED_BATCH_SIZE + 1
        log(f"  배치 {batch_idx}/{total_batches}: {len(batch)}개 임베딩 중...")
        vectors = embed_batch(gemini_client, [d["text"] for d in batch])
        collection.add(
            ids=[d["id"] for d in batch],
            documents=[d["text"] for d in batch],
            embeddings=vectors,
            metadatas=[d["metadata"] for d in batch],
        )
        if batch_idx < total_batches:
            time.sleep(SLEEP_BETWEEN_BATCHES)

    log(f"✓ 인덱싱 완료: 총 {collection.count()}개 청크가 DB에 저장됨")


if __name__ == "__main__":
    main()
