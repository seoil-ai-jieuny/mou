"""
server.py
─────────────────────────────────────────────────────
MOU RAG 챗봇 백엔드 (FastAPI + Gemini API + Chroma).

엔드포인트:
- GET  /api/health  : 헬스체크
- POST /api/chat    : { message: str, history: [{role, content}] } → { answer, sources }

실행:
    uvicorn server:app --reload --port 8000
"""

from __future__ import annotations

import os
from typing import Literal

from dotenv import load_dotenv, find_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from google import genai
from google.genai import types
import chromadb


# ─── 설정 ──────────────────────────────────────────
# .env는 chatbot-server/ 안 → 프로젝트 폴더 → 그 상위 폴더 순서로 자동 탐색.
load_dotenv(find_dotenv(usecwd=True))

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
EMBED_MODEL = os.getenv("EMBED_MODEL", "gemini-embedding-001")
EMBED_DIM = int(os.getenv("EMBED_DIM", "768"))
CHAT_MODEL = os.getenv("CHAT_MODEL", "gemini-2.5-flash")
CHROMA_DIR = os.getenv("CHROMA_DIR", "./chroma_db")
CORS_ORIGINS = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",")]
COLLECTION_NAME = "mou_knowledge"

# RAG 파라미터
TOP_K = 5
MIN_DISTANCE = 1.6  # cosine distance가 이보다 크면 "관련 없음"으로 간주


# ─── 시스템 프롬프트 ───────────────────────────────
SYSTEM_PROMPT = """당신은 서일대학교의 산학협력·MOU(협약) 규정 안내 어시스턴트입니다.
답변의 근거는 학교 규정(산학협력 및 MOU 협약 체결·관리 관련 조항)에서 추출한 QA 지식입니다.

규칙:
1. 반드시 아래에 제공된 [컨텍스트] 안의 정보에만 근거해서 답변합니다.
2. 컨텍스트에 답이 없으면 "제공된 규정 자료에는 해당 정보가 없습니다."라고 솔직하게 답하고, 추측하지 마세요.
3. 답변은 한국어로, 간결하고 정확하게 합니다. 규정 조항의 내용(절차, 기준, 요건 등)을 사실 그대로 전달하세요.
4. 규정명, 조항 번호, 금액·기간·비율 등 수치와 고유명사는 컨텍스트에 있는 대로 정확히 옮깁니다.
5. 답변 본문에 "(출처: …)"를 직접 붙이지 마세요. 출처(근거 규정·조항)는 시스템이 별도로 표시합니다.
6. 산학협력·MOU 규정과 무관한 질문(날씨, 일반 상식 등)에는 정중히 "산학협력·MOU 규정 관련 질문에만 답변드릴 수 있습니다."라고 안내합니다.
"""


# ─── FastAPI 앱 ────────────────────────────────────
app = FastAPI(title="MOU RAG Chatbot (Gemini)", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# ─── 전역 클라이언트 ───────────────────────────────
if not GEMINI_API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY가 설정되지 않았습니다. .env 파일을 확인하세요.\n"
        "발급: https://aistudio.google.com/apikey"
    )

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
chroma_client = chromadb.PersistentClient(path=CHROMA_DIR)

# 컬렉션 로드. DB가 있으면 임베딩 없이 그냥 읽는다(방식 B).
# 없으면 ingest를 시도하되, 실패/부분실패해도 서버는 살아있게 한다
# (Render 무료 티어: 임베딩 rate limit으로 ingest가 중단돼도 서버가 죽지 않도록).
try:
    collection = chroma_client.get_collection(COLLECTION_NAME)
    print(f"Chroma DB 로드됨: {collection.count()}개 청크")
except Exception:
    print("Chroma DB 없음 → ingest.py 자동 실행 시도")
    os.system("python ingest.py")
    collection = chroma_client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"},
    )
    cnt = collection.count()
    if cnt == 0:
        print("⚠ 인덱싱된 청크가 0개입니다. (임베딩 rate limit/키 문제 가능) "
              "서버는 떴지만 답변이 '자료 없음'으로 나올 수 있습니다. "
              "`python ingest.py`를 다시 실행하면 이어서 인덱싱됩니다.")
    else:
        print(f"Chroma DB 준비됨: {cnt}개 청크")


# ─── 스키마 ────────────────────────────────────────
class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    history: list[Message] = Field(default_factory=list)


class Source(BaseModel):
    title: str
    source: str
    type: str


class ChatResponse(BaseModel):
    answer: str
    sources: list[Source]


# ─── 헬스 체크 ────────────────────────────────────
@app.get("/api/health")
def health() -> dict:
    return {
        "ok": True,
        "provider": "google-gemini",
        "collection_count": collection.count(),
        "models": {"embed": EMBED_MODEL, "chat": CHAT_MODEL},
    }


# ─── 검색 ─────────────────────────────────────────
def retrieve(question: str, top_k: int = TOP_K) -> tuple[list[str], list[dict], list[float]]:
    """질문 → Gemini 임베딩(RETRIEVAL_QUERY) → Chroma 유사도 검색."""
    resp = gemini_client.models.embed_content(
        model=EMBED_MODEL,
        contents=question,
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY",   # 질문은 QUERY 태스크로
            output_dimensionality=EMBED_DIM,
        ),
    )
    emb = resp.embeddings[0].values

    res = collection.query(
        query_embeddings=[emb],
        n_results=top_k,
        include=["documents", "metadatas", "distances"],
    )
    docs = res["documents"][0] if res["documents"] else []
    metas = res["metadatas"][0] if res["metadatas"] else []
    dists = res["distances"][0] if res["distances"] else []
    return docs, metas, dists


def build_context(docs: list[str], metas: list[dict]) -> str:
    parts = []
    for i, (d, m) in enumerate(zip(docs, metas), 1):
        title = m.get("title") or m.get("source") or f"문서 {i}"
        parts.append(f"[문서 {i} · {title}]\n{d}")
    return "\n\n---\n\n".join(parts)


# ─── 히스토리 → Gemini contents 변환 ──────────────
# Gemini는 OpenAI와 달리 role="assistant"가 아닌 role="model"을 사용합니다.
def to_gemini_contents(history: list[Message], final_user_text: str) -> list[types.Content]:
    contents: list[types.Content] = []
    for h in history[-8:]:  # 최근 8턴만
        role = "model" if h.role == "assistant" else "user"
        contents.append(types.Content(role=role, parts=[types.Part.from_text(text=h.content)]))
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=final_user_text)]))
    return contents


# ─── 채팅 엔드포인트 ──────────────────────────────
@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    question = req.message.strip()
    if not question:
        raise HTTPException(status_code=400, detail="message가 비어있습니다.")

    # 1. RAG 검색
    try:
        docs, metas, dists = retrieve(question)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"검색 실패: {e}")

    # 거리가 너무 멀면 컨텍스트 비우기 (LLM이 모른다고 답하도록)
    relevant_pairs = [(d, m, dist) for d, m, dist in zip(docs, metas, dists) if dist <= MIN_DISTANCE]

    if not relevant_pairs:
        context = "(관련된 MOU 자료 없음)"
        sources_out: list[Source] = []
    else:
        rel_docs = [p[0] for p in relevant_pairs]
        rel_metas = [p[1] for p in relevant_pairs]
        context = build_context(rel_docs, rel_metas)
        seen = set()
        sources_out = []
        for m in rel_metas:
            key = m.get("title") or m.get("source")
            if key and key not in seen:
                seen.add(key)
                sources_out.append(Source(
                    title=m.get("title", ""),
                    source=m.get("source", ""),
                    type=m.get("type", ""),
                ))

    # 2. Gemini 호출
    final_user_msg = f"[컨텍스트]\n{context}\n\n[질문]\n{question}"
    contents = to_gemini_contents(req.history, final_user_msg)

    try:
        completion = gemini_client.models.generate_content(
            model=CHAT_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                temperature=0.3,
                max_output_tokens=600,
            ),
        )
        answer = (completion.text or "").strip()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Gemini 호출 실패: {e}")

    if not answer:
        answer = "응답을 생성하지 못했습니다. 잠시 후 다시 시도해주세요."

    return ChatResponse(answer=answer, sources=sources_out)


# ─── 직접 실행 ────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
