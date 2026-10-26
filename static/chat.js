// 채팅 화면 동작.

let currentThreadId = null;

const $messages = document.getElementById("messages");
const $form = document.getElementById("chat-form");
const $input = document.getElementById("message");
const $send = document.getElementById("send");
const $error = document.getElementById("error");
const $threadList = document.getElementById("thread-list");


function setActiveThread(threadId) {
  const items = $threadList.querySelectorAll("li");

  items.forEach((item) => {
    item.setAttribute(
      "aria-current",
      item.dataset.threadId === threadId ? "true" : "false"
    );
  });
}

function addBubble(role, text) {
  const el = document.createElement("div");
  el.className = `bubble ${role}`;
  el.textContent = text;
  $messages.appendChild(el);
  $messages.scrollTop = $messages.scrollHeight;
  return el;
}


async function loadThreads() {
  const res = await fetch("/api/threads");

  if (res.status === 401) {
    location.href = "/login";
    return [];
  }

  if (!res.ok) {
    throw new Error(
      await addRequestIdToServerError(
        res,
        "채팅방 목록을 불러오지 못했습니다."
      )
    );
  }

  const threads = await res.json();

  renderThreads(threads);

  return threads;
}

async function loadMessages(threadId) {
  const res = await fetch(`/api/threads/${threadId}/messages`);

  if (res.status === 401) {
    location.href = "/login";
    return;
  }

  if (currentThreadId !== threadId) {
    return;
  }

  if (res.status === 404) {
    currentThreadId = null;
    $messages.innerHTML = "";

    await loadThreads();
    setActiveThread(null);

    throw new Error("삭제되었거나 접근할 수 없는 채팅방입니다.");
  }

  if (!res.ok) {
    throw new Error(
      await addRequestIdToServerError(
        res,
        "이전 대화를 불러오지 못했습니다."
      )
    );
  }

  const logs = await res.json();

  if (currentThreadId !== threadId) {
    return;
  }

  $messages.innerHTML = "";

  logs.forEach((log) => {
    addBubble("user", log.question);

    if (log.answer !== null) {
      addBubble("assistant", log.answer);
    } else {
      addBubble(
        "assistant",
        "이 응답은 생성에 실패했습니다. 다시 질문해 주세요."
      );
    }
  });
}

async function ensureThread() {
  if (currentThreadId) return currentThreadId;

  const res = await fetch("/api/threads", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({}),
  });

  if (res.status === 401) {
    location.href = "/login";
    return null;
  }

  if (!res.ok) {
    throw new Error(
      await addRequestIdToServerError(
        res,
        "대화를 시작하지 못했습니다."
      )
    );
  }

  const thread = await res.json();
  currentThreadId = thread.id;
  return currentThreadId;
}

async function renameThread(threadId, title) {
  const res = await fetch(`/api/threads/${threadId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title }),
  });

  if (res.status === 401) {
    location.href = "/login";
    return false;
  }

  if (res.status === 404) {
    if (currentThreadId === threadId) {
      currentThreadId = null;
      $messages.innerHTML = "";
    }

    await loadThreads();
    setActiveThread(currentThreadId);

    throw new Error("삭제되었거나 접근할 수 없는 채팅방입니다.");
  }

  if (!res.ok) {
    throw new Error(
      await addRequestIdToServerError(
        res,
        "채팅방 제목을 변경하지 못했습니다."
      )
    );
  }

  await res.json();
  return true;
}

async function deleteThread(threadId) {
  const res = await fetch(`/api/threads/${threadId}`, {
    method: "DELETE",
  });

  if (res.status === 401) {
    location.href = "/login";
    return false;
  }

  if (res.status === 404) {
    if (currentThreadId === threadId) {
      currentThreadId = null;
      $messages.innerHTML = "";
    }

    await loadThreads();
    setActiveThread(currentThreadId);

    throw new Error("삭제되었거나 접근할 수 없는 채팅방입니다.");
  }

  if (!res.ok) {
    throw new Error(
      await addRequestIdToServerError(
        res,
        "채팅방을 삭제하지 못했습니다."
      )
    );
  }

  return true;
}


function showError(message) {
  $error.textContent = message;
  $error.hidden = false;
}

async function addRequestIdToServerError(res, message, body = null) {
  // 5xx 오류에만 request_id를 표시한다.
  if (res.status < 500 || res.status >= 600) {
    return message;
  }

  const errorBody =
    body ?? (await res.json().catch(() => ({})));

  const requestId =
    res.headers.get("X-Request-ID") || errorBody.request_id;

  return requestId
    ? `${message} (request_id: ${requestId})`
    : message;
}

function getChatErrorMessage(status, body = {}) {
  if (status === 409) {
    return "이 대화는 현재 응답을 생성 중입니다. 잠시 후 다시 시도해 주세요.";
  }

  if (status === 502) {
    return "AI 응답을 받지 못했어요. 잠시 후 다시 시도해 주세요.";
  }

  if (status === 504) {
    return "현재 응답이 지연되고 있어요. 잠시 후 다시 시도해 주세요.";
  }

  return body.message || "요청을 처리하지 못했습니다.";
}


function renderThreads(threads) {
  $threadList.innerHTML = "";

  threads.forEach((thread) => {
    const item = document.createElement("li");
    item.dataset.threadId = thread.id;

    const title = document.createElement("span");
    title.className = "thread-title";
    title.textContent = thread.title;

    const renameButton = document.createElement("button");
    renameButton.type = "button";
    renameButton.className = "thread-rename";
    renameButton.textContent = "이름 변경";

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.className = "thread-delete";
    deleteButton.textContent = "삭제";

    renameButton.addEventListener("click", async (e) => {
      e.stopPropagation();

      const newTitle = prompt("새 채팅방 제목을 입력하세요.", thread.title);

      if (newTitle === null) return;

      const trimmedTitle = newTitle.trim();

      if (!trimmedTitle) {
        showError("채팅방 제목을 입력해 주세요.");
        return;
      }

      try {
        $error.hidden = true;

        const renamed = await renameThread(thread.id, trimmedTitle);

        if (!renamed) {
          return;
        }

        await loadThreads();
        setActiveThread(currentThreadId);
      } catch (err) {
        showError(err.message || "채팅방 제목을 변경하지 못했습니다.");
      }
    });

    deleteButton.addEventListener("click", async (e) => {
      e.stopPropagation();

      const confirmed = confirm(`"${thread.title}" 채팅방을 삭제하시겠습니까?`);

      if (!confirmed) return;

      try {
        $error.hidden = true;

        const deleted = await deleteThread(thread.id);

        if (!deleted) {
          return;
        }

        if (currentThreadId === thread.id) {
          currentThreadId = null;
          $messages.innerHTML = "";
          $input.value = "";
        }

        await loadThreads();
        setActiveThread(currentThreadId);
        $input.focus();
      } catch (err) {
        showError(err.message || "채팅방을 삭제하지 못했습니다.");
      }
    });

    item.addEventListener("click", async () => {
      try {
        $error.hidden = true;

        currentThreadId = thread.id;
        setActiveThread(thread.id);
        $send.disabled = true;
        $messages.innerHTML = "";

        await loadMessages(thread.id);
      } catch (err) {
        showError(err.message || "이전 대화를 불러오지 못했습니다.");
      } finally {
        if (currentThreadId === thread.id || currentThreadId === null) {
          $send.disabled = false;
        }
      }
    });

    item.appendChild(title);
    item.appendChild(renameButton);
    item.appendChild(deleteButton);
    $threadList.appendChild(item);
  });
}


$form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $input.value.trim();
  if (!text) return;

  $error.hidden = true;
  // 응답을 기다리는 동안 중복 전송을 막는다.
  $send.disabled = true;
  $input.value = "";
  addBubble("user", text);

  const pending = addBubble("assistant pending", "답변을 작성하고 있어요...");

  try {
    const threadId = await ensureThread();

    if (!threadId) {
      return;
    }

    const res = await fetch(`/api/threads/${threadId}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });

    const body = await res.json().catch(() => ({}));

    if (res.status === 401) {
      location.href = "/login";
      return;
    }
    if (!res.ok) {
      pending.remove();

      const message = getChatErrorMessage(res.status, body);

      showError(
        await addRequestIdToServerError(res, message, body)
      );
      return;
    }

    // 툴 실행 승인을 기다리면(휴먼인더루프) 안내 문장이 답으로 온다. 사용자는 채팅으로 "네"/"아니요"를 보낸다.
    pending.className = "bubble assistant";
    pending.textContent = body.answer;

    await loadThreads();
    setActiveThread(currentThreadId);

  } catch (err) {
    pending.remove();
    showError(err.message || "네트워크 오류가 발생했습니다.");
  } finally {
    $send.disabled = false;
    $input.focus();
  }
});


document.getElementById("logout").addEventListener("click", async () => {
  await fetch("/api/auth/logout", { method: "POST" });
  location.href = "/login";
});

// "+ 새 대화": 현재 대화방 선택을 풀고 화면을 비운다. 방은 첫 메시지를 보낼 때 만든다.
document.getElementById("new-thread").addEventListener("click", () => {
  currentThreadId = null;
  $messages.innerHTML = "";
  $error.hidden = true;
  $input.value = "";
  setActiveThread(null);
  $input.focus();
});


loadThreads().catch((err) => {
  showError(err.message || "채팅방 목록을 불러오지 못했습니다.");
});

