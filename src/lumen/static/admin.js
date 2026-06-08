const state = {
  activeTab: "dashboard",
  summary: null,
  dbOverview: null,
  memoryFacts: [],
  editingMemoryFact: null,
  memoryFilter: "all",
  systemLogs: [],
  dbView: "recent_memory",
  activeKnowledgeDocumentId: null,
  chatConversationId: `web-ui-${Date.now()}`,
  chatSessionId: `session-${Date.now()}`,
  pendingAction: null,
};

const uiText = {
  tabs: {
    dashboard: "Головна",
    memory: "Пам'ять",
    knowledge: "Сховище Знань",
    database: "База Даних",
  },
};

async function fetchJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `HTTP ${response.status}`);
  }
  return response.json();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll("\"", "&quot;")
    .replaceAll("'", "&#39;");
}

function pushSystemLog(message, tone = "info") {
  state.systemLogs.unshift({
    message,
    tone,
    time: new Date().toLocaleTimeString(),
  });
  state.systemLogs = state.systemLogs.slice(0, 80);
  renderSystemConsole();
}

function toneForStatus(status) {
  if (status === "ok" || status === "completed" || status === "online") {
    return "tone-ok";
  }
  if (status === "running" || status === "degraded" || status === "unavailable") {
    return "tone-warn";
  }
  return "tone-bad";
}

function setRingProgress(elementId, percent, color) {
  const element = document.getElementById(elementId);
  const clamped = Math.max(0, Math.min(100, percent || 0));
  const degrees = Math.round((clamped / 100) * 360);
  element.style.background = `conic-gradient(${color} ${degrees}deg, rgba(255,255,255,0.08) ${degrees}deg)`;
}

async function loadSummary() {
  state.summary = await fetchJson("/admin/summary");
  renderSummary();
}

async function loadDatabaseOverview() {
  state.dbOverview = await fetchJson("/admin/database/overview?limit=12");
  renderDatabaseOverview();
  renderKnowledgeDocuments();
}

async function loadMemoryFacts() {
  const payload = await fetchJson("/admin/memory/facts?limit=120");
  state.memoryFacts = payload.items || [];
  renderMemoryEntries();
}

function renderSummary() {
  const summary = state.summary;
  if (!summary) {
    return;
  }

  const cards = [
    ["Пам'ять", summary.counts.memory_facts],
    ["Чати", summary.counts.conversation_logs],
    ["Документи", summary.counts.knowledge_documents],
    ["Фрагменти", summary.counts.knowledge_chunks],
    ["Інгестії", summary.counts.ingestion_runs],
  ];

  document.getElementById("summary-cards").innerHTML = cards.map(([label, value]) => `
    <article class="card">
      <h3>${escapeHtml(label)}</h3>
      <strong>${escapeHtml(value)}</strong>
    </article>
  `).join("");

  const statusEl = document.getElementById("summary-status");
  statusEl.textContent = summary.status;
  statusEl.className = `status-pill ${toneForStatus(summary.status)}`;

  document.getElementById("current-model-tag").textContent = summary.current_model || "unknown";
  const metaRows = [
    ["База даних", summary.database_path],
    ["Шляхи знань", summary.knowledge_paths.join(", ") || "не налаштовано"],
    ["Залежності", Object.entries(summary.dependencies).map(([key, value]) => `${key}: ${value}`).join(" | ")],
    ["Остання інгестія", summary.last_ingestion ? `${summary.last_ingestion.status} | ${summary.last_ingestion.documents_indexed} docs` : "немає"],
  ];

  document.getElementById("summary-meta").innerHTML = metaRows.map(([label, value]) => `
    <div class="meta-item"><strong>${escapeHtml(label)}</strong><br>${escapeHtml(value)}</div>
  `).join("");
}

function renderSystemConsole() {
  const container = document.getElementById("system-console");
  container.innerHTML = state.systemLogs.length
    ? state.systemLogs.map((entry) => `
      <div class="console-entry ${toneForStatus(entry.tone)}">
        <strong>[${escapeHtml(entry.time)}]</strong> ${escapeHtml(entry.message)}
      </div>
    `).join("")
    : "<div class='console-entry'>Очікування подій ядра...</div>";
}

function renderKnowledgeDocuments() {
  const docs = state.dbOverview?.recent_documents || [];
  document.getElementById("knowledge-count-inline").textContent = String(docs.length);
  document.getElementById("knowledge-documents").innerHTML = docs.length
    ? docs.map((doc) => `
      <div class="knowledge-doc-card">
        <div class="knowledge-doc-top">
          <strong>${escapeHtml(doc.title)}</strong>
          <div class="knowledge-doc-actions">
            <button class="memory-action" type="button" data-preview-document="${doc.id}">Переглянути</button>
            <button class="memory-action purge" type="button" data-delete-document="${doc.id}">Видалити</button>
          </div>
        </div>
        <div>${escapeHtml(doc.source_type)} | ${escapeHtml(doc.updated_at)}</div>
        <div>${escapeHtml(doc.source_ref)}</div>
      </div>
    `).join("")
    : "<div class='error-box'>Документи ще не індексовано.</div>";

  document.querySelectorAll("[data-preview-document]").forEach((button) => {
    button.addEventListener("click", async () => {
      await previewKnowledgeDocument(Number(button.dataset.previewDocument));
    });
  });

  document.querySelectorAll("[data-delete-document]").forEach((button) => {
    button.addEventListener("click", async () => {
      const documentId = Number(button.dataset.deleteDocument);
      await deleteKnowledgeDocument(documentId);
    });
  });

  if (!docs.length) {
    document.getElementById("knowledge-preview").textContent = "Документи ще не індексовано.";
    document.getElementById("knowledge-preview").className = "knowledge-preview empty";
    state.activeKnowledgeDocumentId = null;
  } else if (state.activeKnowledgeDocumentId && docs.some((doc) => doc.id === state.activeKnowledgeDocumentId)) {
    void previewKnowledgeDocument(state.activeKnowledgeDocumentId, false);
  } else {
    document.getElementById("knowledge-preview").textContent = "Оберіть документ, щоб переглянути вміст.";
    document.getElementById("knowledge-preview").className = "knowledge-preview empty";
    state.activeKnowledgeDocumentId = null;
  }
}

async function previewKnowledgeDocument(documentId, logSelection = true) {
  const response = await fetchJson(`/admin/knowledge/documents/${documentId}`);
  const item = response.item;
  state.activeKnowledgeDocumentId = documentId;
  const preview = document.getElementById("knowledge-preview");
  preview.className = "knowledge-preview";
  preview.innerHTML = `
    <div class="knowledge-preview-head">
      <strong>${escapeHtml(item.title)}</strong>
      <span class="micro-tag">${escapeHtml(item.source_type)}</span>
    </div>
    <div class="knowledge-preview-path">${escapeHtml(item.source_ref)}</div>
    <pre class="knowledge-preview-body">${escapeHtml(item.content || "")}</pre>
  `;
  if (logSelection) {
    pushSystemLog(`Відкрито документ знань: ${item.title}.`, "info");
  }
}

async function deleteKnowledgeDocument(documentId) {
  const response = await fetchJson(`/admin/knowledge/documents/${documentId}`, {
    method: "DELETE",
  });
  pushSystemLog(`Видалено документ знань #${response.deleted_id}.`, "action");
  if (state.activeKnowledgeDocumentId === documentId) {
    state.activeKnowledgeDocumentId = null;
    const preview = document.getElementById("knowledge-preview");
    preview.textContent = "Оберіть документ, щоб переглянути вміст.";
    preview.className = "knowledge-preview empty";
  }
  await Promise.all([loadSummary(), loadDatabaseOverview()]);
}

function getMemoryCategoryClass(category) {
  if (category === "profile") {
    return "category-profile";
  }
  if (category === "device_alias") {
    return "category-device_alias";
  }
  if (category === "rule") {
    return "category-rule";
  }
  return "category-preference";
}

function filteredMemoryFacts() {
  const query = document.getElementById("memory-query").value.trim().toLowerCase();
  return state.memoryFacts.filter((item) => {
    const matchesCategory = state.memoryFilter === "all" || item.category === state.memoryFilter;
    const haystack = [
      item.category,
      item.subject,
      item.predicate,
      item.value,
      item.source_ref,
      ...(item.tags || []),
    ].join(" ").toLowerCase();
    return matchesCategory && (!query || haystack.includes(query));
  });
}

function renderMemoryEntries() {
  const rows = filteredMemoryFacts();
  document.getElementById("memory-match-count").textContent = `${rows.length} записів`;

  document.getElementById("memory-card-grid").innerHTML = rows.length
    ? rows.map((item) => {
      const categoryClass = getMemoryCategoryClass(item.category);
      const tags = (item.tags || []).length ? item.tags.map((tag) => `<span class="memory-tag">${escapeHtml(tag)}</span>`).join("") : "<span class='memory-tag muted'>без тегів</span>";
      return `
        <article class="memory-entry ${categoryClass}">
          <div class="memory-entry-top">
            <div class="memory-entry-title">
              <span class="memory-category ${categoryClass}">${escapeHtml(item.category)}</span>
              <strong>${escapeHtml(item.subject)} ${escapeHtml(item.predicate)}</strong>
            </div>
            <div class="memory-actions">
              <button class="memory-action" type="button" data-edit-memory="${item.id}">EDIT</button>
              <button class="memory-action purge" type="button" data-delete-memory="${item.id}">PURGE</button>
            </div>
          </div>
          <div class="memory-entry-text">${escapeHtml(item.value)}</div>
          <div class="memory-entry-bottom">
            <span>джерело: ${escapeHtml(item.source_ref)}</span>
            <span>важливість ${escapeHtml(item.importance)} | впевненість ${escapeHtml(item.confidence)}</span>
            <div class="memory-tag-row">${tags}</div>
          </div>
        </article>
      `;
    }).join("")
    : "<div class='error-box'>Жоден факт не збігається з поточним фільтром.</div>";

  document.querySelectorAll("[data-edit-memory]").forEach((button) => {
    button.addEventListener("click", () => {
      const fact = state.memoryFacts.find((item) => item.id === Number(button.dataset.editMemory));
      if (!fact) {
        return;
      }
      setMemoryEditorForm(fact);
    });
  });

  document.querySelectorAll("[data-delete-memory]").forEach((button) => {
    button.addEventListener("click", async () => {
      const factId = Number(button.dataset.deleteMemory);
      await fetchJson(`/admin/memory/facts/${factId}`, { method: "DELETE" });
      pushSystemLog(`Стерто факт пам'яті #${factId}.`, "error");
      await Promise.all([loadMemoryFacts(), loadSummary(), loadDatabaseOverview()]);
    });
  });
}

function setMemoryEditorForm(item = null) {
  state.editingMemoryFact = item ?? null;
  document.getElementById("memory-id").value = item?.id ?? "";
  document.getElementById("memory-value").value = item?.value ?? "";
  document.getElementById("memory-save-button").textContent = item ? "Оновити пам'ять" : "Додати пам'ять";
}

function collectMemoryPayload() {
  const fact = state.editingMemoryFact;
  return {
    category: fact?.category ?? "rule",
    subject: fact?.subject ?? "user",
    predicate: fact?.predicate ?? "remember",
    value: document.getElementById("memory-value").value.trim(),
    confidence: Number(fact?.confidence ?? 0.8),
    importance: Number(fact?.importance ?? 6),
    source_ref: fact?.source_ref ?? "admin:manual",
    tags: fact?.tags ?? [],
    expires_at: fact?.expires_at ?? null,
  };
}

function renderDatabaseOverview() {
  const overview = state.dbOverview;
  if (!overview) {
    return;
  }

  document.getElementById("database-counts").innerHTML = Object.entries(overview.table_counts).map(([table, count]) => `
    <div class="db-chip">
      <span>${escapeHtml(table)}</span>
      <strong>${escapeHtml(count)}</strong>
    </div>
  `).join("");

  renderDatabaseInspector();
}

function renderDatabaseInspector() {
  const overview = state.dbOverview;
  if (!overview) {
    return;
  }

  const rows = overview[state.dbView] || [];
  document.getElementById("database-inspector").innerHTML = rows.length
    ? rows.map((row) => `
      <div class="db-row">
        <strong>${escapeHtml(row.title || row.source_path || row.subject || row.category || row.id)}</strong>
        <div>${escapeHtml(JSON.stringify(row))}</div>
      </div>
    `).join("")
    : "<div class='db-row'>Для цього режиму немає рядків.</div>";
}

function activateTab(tabId) {
  state.activeTab = tabId;
  document.querySelectorAll("[data-tab-button]").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.tabButton === tabId);
  });
  document.querySelectorAll("[data-tab-panel]").forEach((panel) => {
    panel.classList.toggle("is-active", panel.dataset.tabPanel === tabId);
  });
  document.getElementById("active-view-title").textContent = uiText.tabs[tabId] || uiText.tabs.dashboard;
  document.querySelector(".main-stage").classList.toggle("memory-mode", tabId === "memory");
  document.querySelector(".main-stage").classList.toggle("knowledge-mode", tabId === "knowledge");
  document.querySelector(".main-stage").classList.toggle("database-mode", tabId === "database");
}

function addChatMessage(role, text) {
  const history = document.getElementById("chat-history");
  const wrapper = document.createElement("div");
  wrapper.className = `chat-message ${role}`;
  wrapper.innerHTML = `
    <div class="chat-author">${role === "assistant" ? "LUMEN_CORE_v4" : "VLAD // USER"}</div>
    <div class="chat-bubble">${escapeHtml(text)}</div>
  `;
  history.appendChild(wrapper);
  history.scrollTop = history.scrollHeight;
  return wrapper.querySelector(".chat-bubble");
}

function setThoughtLog(lines, stateLabel) {
  const panel = document.getElementById("chat-thought-log");
  const content = document.getElementById("chat-thought-log-content");
  document.getElementById("thought-state").textContent = stateLabel;
  content.innerHTML = lines.map((line) => `<div>${escapeHtml(line)}</div>`).join("");
  panel.classList.remove("hidden");
}

function hideThoughtLog() {
  document.getElementById("chat-thought-log").classList.add("hidden");
}

async function typeAssistantMessage(text) {
  const bubble = addChatMessage("assistant", "");
  for (let index = 0; index < text.length; index += 1) {
    bubble.textContent += text[index];
    if (index % 5 === 0) {
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
  }
}

function renderPendingAction(response) {
  const panel = document.getElementById("chat-action-panel");
  if (!response.requires_confirmation || !response.action_proposal) {
    panel.classList.add("hidden");
    panel.innerHTML = "";
    state.pendingAction = null;
    return;
  }

  state.pendingAction = response.action_proposal;
  panel.classList.remove("hidden");
  panel.innerHTML = `
    <strong>${escapeHtml(response.action_proposal.label)}</strong>
    <div>${escapeHtml(response.action_proposal.reason)}</div>
    <div class="chat-action-actions">
      <button class="action-primary" id="confirm-chat-action" type="button">Confirm</button>
      <button class="action-secondary" id="cancel-chat-action" type="button">Cancel</button>
    </div>
  `;
  document.getElementById("confirm-chat-action").addEventListener("click", () => resolvePendingAction(true));
  document.getElementById("cancel-chat-action").addEventListener("click", () => resolvePendingAction(false));
}

async function resolvePendingAction(confirmed) {
  if (!state.pendingAction) {
    return;
  }

  setThoughtLog([
    "pending_action.lookup",
    `action_id=${state.pendingAction.action_id}`,
    `confirmed=${confirmed}`,
  ], confirmed ? "підтвердження" : "скасування");

  const response = await fetchJson("/chat/confirm-action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      action_id: state.pendingAction.action_id,
      confirmed,
      conversation_id: state.chatConversationId,
      user_id: "web-admin",
    }),
  });

  hideThoughtLog();
  document.getElementById("chat-action-panel").classList.add("hidden");
  await typeAssistantMessage(response.answer);
  pushSystemLog(`Завершено обробку дії: ${state.pendingAction.label}.`, confirmed ? "action" : "warn");
  state.pendingAction = null;
  await Promise.all([loadMemoryFacts(), loadSummary(), loadDatabaseOverview()]);
}

async function submitChat(event) {
  event.preventDefault();
  const input = document.getElementById("chat-input");
  const text = input.value.trim();
  if (!text) {
    return;
  }

  addChatMessage("user", text);
  input.value = "";
  setThoughtLog([
    "context.memory.search",
    "context.knowledge.search",
    "lumen.agent.invoke",
  ], "thinking");
  pushSystemLog(`Черга чату: ${text}`, "info");

  const response = await fetchJson("/chat/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      text,
      source: "web_admin_ui",
      user_id: "web-admin",
      session_id: state.chatSessionId,
      conversation_id: state.chatConversationId,
      allow_actions: true,
    }),
  });

  hideThoughtLog();
  await typeAssistantMessage(response.answer);
  renderPendingAction(response);
  pushSystemLog(`Агент відповів: ${response.memory_hits.length} memory hits і ${response.knowledge_hits.length} knowledge hits.`, "search");
  await Promise.all([loadMemoryFacts(), loadSummary()]);
}

async function uploadKnowledgeFiles(event) {
  if (event) {
    event.preventDefault();
  }

  const input = document.getElementById("knowledge-files");
  const files = Array.from(input.files || []);
  const log = document.getElementById("upload-status");
  if (!files.length) {
    log.textContent = "Обери хоча б один файл.";
    return;
  }

  const relativePath = document.getElementById("relative-path").value.trim();
  const reindexAfterUpload = document.getElementById("reindex-after-upload").checked;
  const messages = [];

  for (const file of files) {
    log.textContent = `Інгестія ${file.name}...`;
    const result = await fetchJson("/admin/knowledge/upload", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: file.name,
        content: await file.text(),
        relative_path: relativePath || null,
        reindex_after_upload: reindexAfterUpload,
      }),
    });
    messages.push(`${file.name} -> indexed ${result.indexed_documents}`);
    pushSystemLog(`Завершено завантаження знань для ${file.name}.`, "action");
  }

  log.textContent = messages.join("\n");
  input.value = "";
  await Promise.all([loadSummary(), loadDatabaseOverview()]);
}

async function saveMemoryFact(event) {
  event.preventDefault();
  const factId = document.getElementById("memory-id").value.trim();
  const memoryText = document.getElementById("memory-value").value.trim();
  if (!memoryText) {
    return;
  }

  let result;
  if (factId) {
    result = await fetchJson(`/admin/memory/facts/${factId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectMemoryPayload()),
    });
    pushSystemLog(`Оновлено факт пам'яті #${result.item.id}.`, "action");
  } else {
    result = await fetchJson("/admin/memory/facts/from-text", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text: memoryText,
        source_ref: "admin:manual",
      }),
    });
    pushSystemLog(`Створено ${result.items.length} запис(ів) пам'яті з тексту.`, "action");
  }

  setMemoryEditorForm(null);
  await Promise.all([loadMemoryFacts(), loadSummary(), loadDatabaseOverview()]);
}

async function refreshAll() {
  const results = await Promise.allSettled([
    loadSummary(),
    loadDatabaseOverview(),
    loadMemoryFacts(),
  ]);

  if (results.some((result) => result.status === "rejected")) {
    const messages = results
      .filter((result) => result.status === "rejected")
      .map((result) => result.reason.message);
    pushSystemLog(`Оновлення завершилось з помилками: ${messages.join(" | ")}`, "error");
  } else {
    pushSystemLog("Оновлення системи завершено.", "action");
  }
}

function attachStaticEvents() {
  document.getElementById("refresh-all").addEventListener("click", refreshAll);
  document.getElementById("clear-console").addEventListener("click", () => {
    state.systemLogs = [];
    renderSystemConsole();
  });

  document.getElementById("memory-search-form").addEventListener("submit", (event) => {
    event.preventDefault();
    renderMemoryEntries();
  });
  document.getElementById("memory-query").addEventListener("input", renderMemoryEntries);
  document.querySelectorAll("[data-memory-filter]").forEach((button) => {
    button.addEventListener("click", () => {
      state.memoryFilter = button.dataset.memoryFilter;
      document.querySelectorAll("[data-memory-filter]").forEach((item) => item.classList.remove("is-active"));
      button.classList.add("is-active");
      renderMemoryEntries();
    });
  });

  document.getElementById("memory-editor-form").addEventListener("submit", saveMemoryFact);

  document.getElementById("upload-form").addEventListener("submit", uploadKnowledgeFiles);
  document.querySelectorAll("[data-db-view]").forEach((button) => {
    button.addEventListener("click", () => {
      state.dbView = button.dataset.dbView;
      document.querySelectorAll("[data-db-view]").forEach((item) => item.classList.remove("is-active"));
      button.classList.add("is-active");
      renderDatabaseInspector();
    });
  });

  document.querySelectorAll("[data-tab-button]").forEach((button) => {
    button.addEventListener("click", () => activateTab(button.dataset.tabButton));
  });

  document.getElementById("chat-form").addEventListener("submit", submitChat);
  document.getElementById("clear-chat").addEventListener("click", () => {
    document.getElementById("chat-history").innerHTML = "";
    state.pendingAction = null;
    document.getElementById("chat-action-panel").classList.add("hidden");
    addChatMessage("assistant", "Канал розмови очищено. Очікую нових інструкцій.");
  });

  const dropZone = document.getElementById("drop-zone");
  ["dragenter", "dragover"].forEach((eventName) => {
    dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropZone.classList.add("is-dragging");
    });
  });
  ["dragleave", "drop"].forEach((eventName) => {
    dropZone.addEventListener(eventName, (event) => {
      event.preventDefault();
      dropZone.classList.remove("is-dragging");
    });
  });
  dropZone.addEventListener("drop", async (event) => {
    const files = Array.from(event.dataTransfer?.files || []);
    const input = document.getElementById("knowledge-files");
    if (files.length) {
      const dataTransfer = new DataTransfer();
      files.forEach((file) => dataTransfer.items.add(file));
      input.files = dataTransfer.files;
      await uploadKnowledgeFiles();
    }
  });
}

function seedInitialChat() {
  addChatMessage("assistant", "LUMEN core link established. Питай про пам'ять, знання або проси виконати дію в Home Assistant.");
}

attachStaticEvents();
setMemoryEditorForm(null);
seedInitialChat();
pushSystemLog("LUMEN // CORE інтерфейс ініціалізовано.", "action");
refreshAll().catch((error) => {
  pushSystemLog(`Помилка ініціалізації: ${error.message}`, "error");
});
