"use strict";

const state = {
  chats: [],
  activeChat: null,
  filter: "all",
  cursor: null,
  hasMore: false,
  loadingHistory: false,
  loadedMessageIds: new Set(),
  searchTimer: null,
  searchRequest: 0,
};

const elements = {
  shell: document.querySelector(".app-shell"),
  chatList: document.querySelector("#chatList"),
  listTitle: document.querySelector("#listTitle"),
  listCount: document.querySelector("#listCount"),
  sidebarStatus: document.querySelector("#sidebarStatus"),
  searchInput: document.querySelector("#searchInput"),
  emptyState: document.querySelector("#emptyState"),
  chatView: document.querySelector("#chatView"),
  activeAvatar: document.querySelector("#activeAvatar"),
  activeChatName: document.querySelector("#activeChatName"),
  activeChatMeta: document.querySelector("#activeChatMeta"),
  messages: document.querySelector("#messages"),
  messageList: document.querySelector("#messageList"),
  historyLoader: document.querySelector("#historyLoader"),
  themeButton: document.querySelector("#themeButton"),
  themeIcon: document.querySelector("#themeIcon"),
  backButton: document.querySelector("#backButton"),
  jumpBottomButton: document.querySelector("#jumpBottomButton"),
  lightbox: document.querySelector("#lightbox"),
  lightboxImage: document.querySelector("#lightboxImage"),
  lightboxCaption: document.querySelector("#lightboxCaption"),
  lightboxClose: document.querySelector("#lightboxClose"),
  toast: document.querySelector("#toast"),
};

function initials(name) {
  return (name || "?").split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]).join("").toUpperCase();
}

function formatListDate(value) {
  if (!value) return "";
  const date = new Date(value);
  const now = new Date();
  if (date.toDateString() === now.toDateString()) return new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit" }).format(date);
  return new Intl.DateTimeFormat("pt-BR", { day: "2-digit", month: "2-digit", year: date.getFullYear() === now.getFullYear() ? undefined : "2-digit" }).format(date);
}

function formatMessageDate(value) {
  return new Intl.DateTimeFormat("pt-BR", { dateStyle: "long" }).format(new Date(value));
}

function formatTime(value) {
  return new Intl.DateTimeFormat("pt-BR", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function toast(message) {
  elements.toast.textContent = message;
  elements.toast.hidden = false;
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => { elements.toast.hidden = true; }, 4000);
}

async function api(path) {
  const response = await fetch(path, { headers: { Accept: "application/json" } });
  if (!response.ok) {
    let detail = `Erro ${response.status}`;
    try { detail = (await response.json()).detail || detail; } catch (_) { /* resposta sem JSON */ }
    throw new Error(detail);
  }
  return response.json();
}

function setSidebarStatus(message = "") {
  elements.sidebarStatus.textContent = message;
  elements.sidebarStatus.hidden = !message;
}

function filteredChats() {
  if (state.filter === "groups") return state.chats.filter((chat) => chat.is_group);
  if (state.filter === "clients") return state.chats.filter((chat) => !chat.is_group);
  return state.chats;
}

function renderChats() {
  elements.chatList.replaceChildren();
  const chats = filteredChats();
  elements.listTitle.textContent = "Conversas";
  elements.listCount.textContent = String(chats.length);
  setSidebarStatus(chats.length ? "" : "Nenhuma conversa neste filtro.");
  for (const chat of chats) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `chat-item${state.activeChat?.jid === chat.jid ? " active" : ""}`;
    button.setAttribute("role", "listitem");
    button.addEventListener("click", () => selectChat(chat));

    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.textContent = initials(chat.name);
    const copy = document.createElement("div");
    copy.className = "chat-copy";
    const nameLine = document.createElement("div");
    nameLine.className = "chat-name-line";
    const name = document.createElement("span");
    name.className = "chat-name";
    name.textContent = chat.name || chat.jid;
    const type = document.createElement("span");
    type.className = "type-icon";
    type.textContent = chat.is_group ? "grupo" : "cliente";
    const preview = document.createElement("div");
    preview.className = "chat-preview";
    preview.textContent = chat.last_message_preview || "Sem mensagens";
    const date = document.createElement("time");
    date.className = "chat-date";
    date.dateTime = chat.last_message_time || "";
    date.textContent = formatListDate(chat.last_message_time);
    nameLine.append(name, type);
    copy.append(nameLine, preview);
    button.append(avatar, copy, date);
    elements.chatList.append(button);
  }
}

function appendSafeHighlight(container, highlighted, fallback) {
  const source = highlighted || fallback || "";
  const parts = source.split(/(<mark>|<\/mark>)/i);
  let mark = null;
  for (const part of parts) {
    if (/^<mark>$/i.test(part)) {
      mark = document.createElement("mark");
      container.append(mark);
    } else if (/^<\/mark>$/i.test(part)) {
      mark = null;
    } else {
      (mark || container).append(document.createTextNode(part));
    }
  }
}

function renderSearchResults(results, chats = []) {
  elements.chatList.replaceChildren();
  elements.listTitle.textContent = "Resultados";
  elements.listCount.textContent = String(results.length + chats.length);
  setSidebarStatus(results.length || chats.length ? "" : "Nenhuma conversa ou mensagem encontrada.");
  for (const chat of chats) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chat-item";
    button.addEventListener("click", () => {
      elements.searchInput.value = "";
      selectChat(chat);
    });
    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.textContent = initials(chat.name);
    const copy = document.createElement("div");
    copy.className = "chat-copy";
    const name = document.createElement("div");
    name.className = "chat-name";
    name.textContent = chat.name || chat.jid;
    const preview = document.createElement("div");
    preview.className = "chat-preview";
    preview.textContent = `Conversa · ${chat.is_group ? "grupo" : "cliente"}`;
    copy.append(name, preview);
    button.append(avatar, copy);
    elements.chatList.append(button);
  }
  for (const result of results) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chat-item search-result";
    button.addEventListener("click", () => openSearchResult(result));
    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.textContent = initials(result.chat_name);
    const copy = document.createElement("div");
    copy.className = "chat-copy";
    const meta = document.createElement("div");
    meta.className = "result-meta";
    meta.textContent = `${result.chat_name || result.message.chat_jid} · ${formatListDate(result.message.timestamp)}`;
    const highlight = document.createElement("div");
    highlight.className = "result-highlight";
    appendSafeHighlight(highlight, result.highlight, result.message.content);
    copy.append(meta, highlight);
    button.append(avatar, copy);
    elements.chatList.append(button);
  }
}

async function loadChats() {
  setSidebarStatus("Carregando conversas…");
  try {
    const chats = [];
    const pageSize = 500;
    while (true) {
      const page = await api(`/api/chats?limit=${pageSize}&offset=${chats.length}`);
      chats.push(...page);
      if (page.length < pageSize) break;
    }
    state.chats = chats;
    renderChats();
  } catch (error) {
    setSidebarStatus("Não foi possível carregar o arquivo.");
    toast(error.message);
  }
}

function mediaFilename(path) {
  if (!path) return "Documento";
  const part = path.split("/").pop();
  try { return decodeURIComponent(part); } catch (_) { return part; }
}

function createMedia(message) {
  if (!message.media_url) return null;
  const wrapper = document.createElement("div");
  wrapper.className = "message-media";
  if (message.media_type === "image" || message.media_type === "sticker") {
    const image = document.createElement("img");
    image.src = message.media_url;
    image.alt = message.content || (message.media_type === "sticker" ? "Figurinha" : "Imagem");
    image.loading = "lazy";
    image.addEventListener("click", () => {
      elements.lightboxImage.src = message.media_url;
      elements.lightboxCaption.textContent = message.content || mediaFilename(message.media_path);
      elements.lightbox.showModal();
    });
    wrapper.append(image);
  } else if (message.media_type === "video") {
    const video = document.createElement("video");
    video.src = message.media_url;
    video.controls = true;
    video.preload = "metadata";
    wrapper.append(video);
  } else if (message.media_type === "audio") {
    const audio = document.createElement("audio");
    audio.src = message.media_url;
    audio.controls = true;
    audio.preload = "metadata";
    wrapper.append(audio);
  } else {
    const link = document.createElement("a");
    link.className = "document-link";
    link.href = message.media_url;
    link.download = mediaFilename(message.media_path);
    link.target = "_blank";
    link.rel = "noopener";
    const icon = document.createElement("span");
    icon.className = "document-icon";
    icon.textContent = message.media_mime === "application/pdf" ? "PDF" : "DOC";
    const name = document.createElement("span");
    name.className = "document-name";
    name.textContent = mediaFilename(message.media_path);
    link.append(icon, name, document.createTextNode("↓"));
    wrapper.append(link);
  }
  return wrapper;
}

function messageElement(message) {
  const row = document.createElement("article");
  row.className = `message-row ${message.from_me ? "sent" : "received"}`;
  row.id = `msg-${message.id}`;
  row.dataset.messageId = message.id;
  const bubble = document.createElement("div");
  bubble.className = "bubble";

  if (!message.from_me && state.activeChat?.is_group && message.sender_name) {
    const sender = document.createElement("div");
    sender.className = "sender";
    sender.textContent = message.sender_name;
    bubble.append(sender);
  }
  if (message.quoted_message_id) {
    const quoted = document.createElement("div");
    quoted.className = "quote";
    quoted.tabIndex = 0;
    const quotedSender = document.createElement("strong");
    quotedSender.textContent = message.quoted_message?.sender_name || "Mensagem citada";
    const quotedContent = document.createElement("span");
    quotedContent.textContent = message.quoted_message?.content || (message.quoted_message?.media_type ? `[${message.quoted_message.media_type}]` : "Conteúdo não disponível");
    quoted.append(quotedSender, quotedContent);
    const goToQuote = () => jumpToMessage(message.quoted_message_id);
    quoted.addEventListener("click", goToQuote);
    quoted.addEventListener("keydown", (event) => { if (event.key === "Enter") goToQuote(); });
    bubble.append(quoted);
  }
  const media = createMedia(message);
  if (media) bubble.append(media);
  if (message.content) {
    const content = document.createElement("div");
    content.className = "message-content";
    content.textContent = message.content;
    bubble.append(content);
  } else if (message.has_media && !message.media_url) {
    const unavailable = document.createElement("div");
    unavailable.className = "message-content";
    unavailable.textContent = `[${message.media_type || "mídia"} não localizada]`;
    bubble.append(unavailable);
  }
  const time = document.createElement("time");
  time.className = "message-time";
  time.dateTime = message.timestamp;
  time.title = new Intl.DateTimeFormat("pt-BR", { dateStyle: "full", timeStyle: "medium" }).format(new Date(message.timestamp));
  time.textContent = formatTime(message.timestamp);
  bubble.append(time);
  row.append(bubble);
  return row;
}

function rebuildMessages(messages) {
  elements.messageList.replaceChildren();
  state.loadedMessageIds.clear();
  appendMessages(messages, false);
}

function appendMessages(messages, prepend) {
  const fragment = document.createDocumentFragment();
  let previousDate = null;
  if (prepend && elements.messageList.firstElementChild?.dataset.date) {
    previousDate = null;
  }
  for (const message of messages) {
    if (state.loadedMessageIds.has(message.id)) continue;
    const dateKey = new Date(message.timestamp).toLocaleDateString("en-CA");
    if (dateKey !== previousDate) {
      const separator = document.createElement("div");
      separator.className = "date-separator";
      separator.dataset.date = dateKey;
      separator.textContent = formatMessageDate(message.timestamp);
      fragment.append(separator);
      previousDate = dateKey;
    }
    fragment.append(messageElement(message));
    state.loadedMessageIds.add(message.id);
  }
  if (prepend) {
    const first = elements.messageList.firstChild;
    elements.messageList.insertBefore(fragment, first);
    deduplicateDateSeparators();
  } else {
    elements.messageList.append(fragment);
  }
}

function deduplicateDateSeparators() {
  let lastDate = null;
  for (const child of [...elements.messageList.children]) {
    if (child.classList.contains("date-separator")) {
      if (child.dataset.date === lastDate) child.remove();
      else lastDate = child.dataset.date;
    }
  }
}

async function selectChat(chat, around = null) {
  state.activeChat = chat;
  state.cursor = null;
  state.hasMore = false;
  state.loadedMessageIds.clear();
  elements.emptyState.hidden = true;
  elements.chatView.hidden = false;
  elements.shell.classList.add("chat-open");
  elements.activeAvatar.textContent = initials(chat.name);
  elements.activeChatName.textContent = chat.name || chat.jid;
  elements.activeChatMeta.textContent = `${chat.is_group ? "Grupo" : "Cliente"} · ${chat.jid}`;
  elements.messageList.replaceChildren();
  renderChats();

  try {
    const suffix = around ? `?around=${encodeURIComponent(around)}&limit=80` : "?limit=80";
    const page = await api(`/api/chats/${encodeURIComponent(chat.jid)}/messages${suffix}`);
    state.cursor = page.next_cursor;
    state.hasMore = page.has_more;
    rebuildMessages(page.items);
    requestAnimationFrame(() => {
      if (around) highlightMessage(around);
      else elements.messages.scrollTop = elements.messages.scrollHeight;
    });
  } catch (error) {
    toast(error.message);
  }
}

async function loadOlderMessages() {
  if (!state.activeChat || !state.hasMore || !state.cursor || state.loadingHistory) return;
  state.loadingHistory = true;
  elements.historyLoader.hidden = false;
  const previousHeight = elements.messages.scrollHeight;
  try {
    const page = await api(`/api/chats/${encodeURIComponent(state.activeChat.jid)}/messages?before=${encodeURIComponent(state.cursor)}&limit=80`);
    state.cursor = page.next_cursor;
    state.hasMore = page.has_more;
    appendMessages(page.items, true);
    elements.messages.scrollTop = elements.messages.scrollHeight - previousHeight;
  } catch (error) {
    toast(error.message);
  } finally {
    state.loadingHistory = false;
    elements.historyLoader.hidden = true;
  }
}

function highlightMessage(messageId) {
  const target = document.getElementById(`msg-${messageId}`);
  if (!target) return false;
  target.scrollIntoView({ block: "center" });
  target.classList.add("targeted");
  window.setTimeout(() => target.classList.remove("targeted"), 2200);
  return true;
}

async function jumpToMessage(messageId) {
  if (highlightMessage(messageId)) return;
  if (state.activeChat) await selectChat(state.activeChat, messageId);
}

async function openSearchResult(result) {
  const chat = state.chats.find((item) => item.jid === result.message.chat_jid) || {
    jid: result.message.chat_jid,
    name: result.chat_name,
    is_group: result.is_group,
  };
  elements.searchInput.value = "";
  await selectChat(chat, result.message.id);
}

async function performSearch(value) {
  const term = value.trim();
  if (term.length < 2) {
    renderChats();
    return;
  }
  const requestId = ++state.searchRequest;
  setSidebarStatus("Buscando no arquivo…");
  try {
    const [page, chats] = await Promise.all([
      api(`/api/search?q=${encodeURIComponent(term)}&limit=100`),
      api(`/api/chats?q=${encodeURIComponent(term)}&limit=30`),
    ]);
    if (requestId === state.searchRequest) renderSearchResults(page.items, chats);
  } catch (error) {
    if (requestId === state.searchRequest) {
      setSidebarStatus("A busca não pôde ser concluída.");
      toast(error.message);
    }
  }
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  elements.themeIcon.textContent = theme === "dark" ? "☀" : "☾";
  localStorage.setItem("wlav-theme", theme);
}

elements.searchInput.addEventListener("input", (event) => {
  window.clearTimeout(state.searchTimer);
  state.searchTimer = window.setTimeout(() => performSearch(event.target.value), 280);
});

document.querySelectorAll(".filter").forEach((button) => {
  button.addEventListener("click", () => {
    state.filter = button.dataset.filter;
    document.querySelectorAll(".filter").forEach((item) => item.classList.toggle("active", item === button));
    elements.searchInput.value = "";
    renderChats();
  });
});

elements.messages.addEventListener("scroll", () => {
  if (elements.messages.scrollTop < 180) loadOlderMessages();
});
elements.themeButton.addEventListener("click", () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
elements.backButton.addEventListener("click", () => elements.shell.classList.remove("chat-open"));
elements.jumpBottomButton.addEventListener("click", () => { elements.messages.scrollTop = elements.messages.scrollHeight; });
elements.lightboxClose.addEventListener("click", () => elements.lightbox.close());
elements.lightbox.addEventListener("click", (event) => { if (event.target === elements.lightbox) elements.lightbox.close(); });
document.addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    elements.searchInput.focus();
  }
});

const preferredTheme = localStorage.getItem("wlav-theme") || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
applyTheme(preferredTheme);
loadChats();
