/* ───────────────────────────────────────────────
   MOU RAG Chatbot Widget · 클라이언트 로직
   - 백엔드 엔드포인트: window.CHATBOT_API_URL (기본 http://localhost:8000/api/chat)
   - 세션별 대화 히스토리 유지 (페이지 새로고침 시 초기화)
   ─────────────────────────────────────────────── */
(function () {
  "use strict";

  // ─── 설정 ───────────────────────────────────────
  const API_URL = window.CHATBOT_API_URL || "http://localhost:8000/api/chat";
  const SUGGESTIONS = [
    "산학협력 협약은 누구 명의로 체결하나요?",
    "현장실습학기제 협약 절차가 궁금해요",
    "협약 내용을 변경하려면 어떻게 하나요?",
  ];

  // ─── DOM 헬퍼 ──────────────────────────────────
  const el = (tag, attrs = {}, children = []) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") node.className = v;
      else if (k === "html") node.innerHTML = v;
      else if (k.startsWith("on") && typeof v === "function") {
        node.addEventListener(k.slice(2), v);
      } else {
        node.setAttribute(k, v);
      }
    }
    for (const c of [].concat(children)) {
      if (typeof c === "string") node.appendChild(document.createTextNode(c));
      else if (c) node.appendChild(c);
    }
    return node;
  };

  // 간단한 마크다운: **bold**, 줄바꿈은 CSS의 white-space: pre-wrap이 처리
  const escapeHtml = (s) =>
    s.replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  const renderBotText = (text) =>
    escapeHtml(text).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");

  // ─── 위젯 빌드 ─────────────────────────────────
  const root = el("div", { class: "chatbot-root", id: "chatbot-root" });

  // 패널
  const messages = el("div", { class: "chatbot-messages", id: "chatbot-messages" });

  const suggestions = el(
    "div",
    { class: "chatbot-suggestions" },
    SUGGESTIONS.map((q) =>
      el("button", {
        class: "chatbot-chip",
        type: "button",
        onclick: () => askQuestion(q),
      }, q)
    )
  );

  const input = el("input", {
    class: "chatbot-input",
    id: "chatbot-input",
    type: "text",
    placeholder: "MOU 관련 질문을 입력하세요...",
    autocomplete: "off",
  });

  const sendBtn = el("button", {
    class: "chatbot-send",
    id: "chatbot-send",
    type: "button",
    "aria-label": "전송",
    html: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>`,
  });

  const panel = el("div", { class: "chatbot-panel", id: "chatbot-panel" }, [
    el("div", { class: "chatbot-header" }, [
      el("div", { class: "chatbot-header-left" }, [
        el("div", { class: "chatbot-status-dot", id: "chatbot-status" }),
        el("div", {}, [
          el("div", { class: "chatbot-title" }, "MOU Assistant"),
        ]),
      ]),
    ]),
    messages,
    suggestions,
    el("div", { class: "chatbot-input-wrap" }, [input, sendBtn]),
  ]);

  // 토글 버튼
  const toggleBtn = el("button", {
    class: "chatbot-toggle",
    id: "chatbot-toggle",
    type: "button",
    "aria-label": "MOU 챗봇 열기",
    "aria-expanded": "false",
    html: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>`,
  });

  root.appendChild(panel);
  root.appendChild(toggleBtn);
  document.body.appendChild(root);

  // ─── 상태 ──────────────────────────────────────
  let isOpen = false;
  let isSending = false;
  const history = []; // [{role: "user"|"assistant", content: "..."}]

  // ─── 토글 ──────────────────────────────────────
  const setOpen = (open) => {
    isOpen = open;
    root.classList.toggle("is-open", open);
    toggleBtn.setAttribute("aria-expanded", String(open));
    toggleBtn.setAttribute("aria-label", open ? "MOU 챗봇 닫기" : "MOU 챗봇 열기");
    if (open) {
      setTimeout(() => input.focus(), 240);
      if (messages.children.length === 0) showGreeting();
    }
  };
  toggleBtn.addEventListener("click", () => setOpen(!isOpen));

  // ─── 메시지 그리기 ─────────────────────────────
  const scrollBottom = () => {
    messages.scrollTop = messages.scrollHeight;
  };

  const addUserMsg = (text) => {
    messages.appendChild(el("div", { class: "chatbot-msg user" }, text));
    scrollBottom();
  };

  const addBotMsg = (text, sources) => {
    const node = el("div", { class: "chatbot-msg bot", html: renderBotText(text) });
    if (sources && sources.length) {
      const srcLine = sources.map((s) => s.title || s.source || "문서").join(" · ");
      node.appendChild(el("div", {
        class: "chatbot-sources",
        html: `<b>// 참고:</b> ${escapeHtml(srcLine)}`,
      }));
    }
    messages.appendChild(node);
    scrollBottom();
  };

  const addErrorMsg = (text) => {
    messages.appendChild(el("div", { class: "chatbot-msg error" }, text));
    scrollBottom();
  };

  const addTyping = () => {
    const node = el("div", { class: "chatbot-msg bot", id: "chatbot-typing-node" }, [
      el("div", {
        class: "chatbot-typing",
        html: "<span></span><span></span><span></span>",
      }),
    ]);
    messages.appendChild(node);
    scrollBottom();
    return node;
  };

  const showGreeting = () => {
    addBotMsg(
      "안녕하세요. MOU/산학협력 어시스턴트입니다.\nMOU 방법 혹은 산학협력 관해 궁금한 사항을 물어보세요."
    );
  };

  // ─── API 호출 ──────────────────────────────────
  async function askQuestion(text) {
    if (isSending) return;
    const q = (text || input.value || "").trim();
    if (!q) return;

    input.value = "";
    isSending = true;
    sendBtn.disabled = true;

    addUserMsg(q);
    history.push({ role: "user", content: q });

    const typingNode = addTyping();

    try {
      const res = await fetch(API_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: q,
          history: history.slice(-10), // 최근 10턴만
        }),
      });

      typingNode.remove();

      if (!res.ok) {
        const errBody = await res.text().catch(() => "");
        throw new Error(`서버 응답 오류 (${res.status}): ${errBody.slice(0, 200)}`);
      }

      const data = await res.json();
      const answer = data.answer || "응답이 비어있습니다.";
      const sources = data.sources || [];

      addBotMsg(answer, sources);
      history.push({ role: "assistant", content: answer });
    } catch (err) {
      typingNode.remove();
      console.error("[chatbot] error:", err);
      addErrorMsg(
        `오류가 발생했습니다. 챗봇 서버(${API_URL})가 실행 중인지 확인해주세요.\n${err.message || err}`
      );
      document.getElementById("chatbot-status")?.classList.add("is-offline");
    } finally {
      isSending = false;
      sendBtn.disabled = false;
      input.focus();
    }
  }

  // ─── 이벤트 ────────────────────────────────────
  sendBtn.addEventListener("click", () => askQuestion());
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.isComposing) {
      e.preventDefault();
      askQuestion();
    }
  });

  // ESC로 닫기
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && isOpen) setOpen(false);
  });

  // ─── 서버 헬스 체크 (낙관적) ──────────────────
  fetch(API_URL.replace(/\/api\/chat$/, "/api/health"), { method: "GET" })
    .then((r) => {
      if (!r.ok) throw new Error("offline");
    })
    .catch(() => {
      document.getElementById("chatbot-status")?.classList.add("is-offline");
    });
})();
