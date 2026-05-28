이 폴더의 JSONL 파일이 챗봇의 단일 지식 출처입니다.

- mou_qa.jsonl  → 인덱싱되는 실제 지식을 수정하려면 .jsonl 을 편집/교체한 뒤:
    python ingest.py --reset