이 폴더는 (선택) 보조 문서용입니다.

★ 현재 챗봇의 기본 지식 출처는 ../chatbot-server/knowledge/ 폴더의
  "규정 QA 세트(산학협력·MOU)" JSONL 파일입니다.
  (기존 index.html 파트너(MOU 카드) 데이터는 더 이상 사용하지 않습니다.)

이 docs/ 폴더에 .md / .txt / .pdf 를 추가하면 보조 지식으로 함께 인덱싱됩니다.

지식을 추가/교체한 뒤에는 아래를 실행하세요(교체 시 --reset 권장):
    python ingest.py --reset
