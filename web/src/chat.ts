import "./chat.css";
import { apiUrl } from "./api";

/** Citizen-facing "WhatsApp" web chat. Talks to the backend /webchat API (a real conversation, not a mock). */

interface ChatButton {
  id: string;
  title: string;
}
interface ChatMessage {
  seq: number;
  direction: "in" | "out";
  kind: "text" | "buttons" | "image" | "location" | "audio" | "system";
  text: string | null;
  buttons: ChatButton[] | null;
  image_url: string | null;
  lat: number | null;
  lon: number | null;
  ts: string | number | null;
  status: string | null;
}

const SESSION_KEY = "willay.webchat.session";
const POLL_MS = 1500;
const TYPING_TIMEOUT_MS = 30_000;
const DEMO_POINT = { lat: -11.7608, lon: -76.3045 }; // San Mateo de Huanchor (Rimac basin), for the recording

const $ = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;
const chat = $<HTMLElement>("wa-chat");
const statusEl = $<HTMLElement>("wa-status");
const offlineEl = $<HTMLElement>("wa-offline");
const input = $<HTMLInputElement>("wa-input");
const sendBtn = $<HTMLButtonElement>("wa-send");
const micBtn = $<HTMLButtonElement>("wa-mic");
const locBtn = $<HTMLButtonElement>("wa-loc");
const form = $<HTMLFormElement>("wa-form");
const sheet = $<HTMLElement>("wa-sheet");
const recBar = $<HTMLElement>("wa-rec");
const recTime = $<HTMLElement>("wa-rec-time");

let sessionId: string | null = null;
let lastSeq = 0;
let polling = false;
let failures = 0;
let pollTimer: number | undefined;
let typingUntil = 0;
let typingEl: HTMLElement | null = null;
const pending: { kind: string; text: string | null; el: HTMLElement }[] = [];
const userBubbles: HTMLElement[] = [];

// ---------- helpers -------------------------------------------------------------------------

function h<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string, text?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function mediaUrl(path: string): string {
  return /^https?:/i.test(path) ? path : apiUrl(path);
}

function clock(ts: string | number | null): string {
  let date = new Date();
  if (typeof ts === "number") date = new Date(ts < 1e12 ? ts * 1000 : ts);
  else if (typeof ts === "string" && ts) {
    const parsed = new Date(ts);
    if (!Number.isNaN(parsed.getTime())) date = parsed;
  }
  return date.toLocaleTimeString("es-PE", { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** Minimal WhatsApp formatting: *bold* and _italic_, built as DOM nodes (never innerHTML). */
function formatInto(parent: HTMLElement, text: string): void {
  const pattern = /(\*[^*\n]+\*|_[^_\n]+_)/g;
  for (const part of text.split(pattern)) {
    if (!part) continue;
    if (part.length > 2 && part.startsWith("*") && part.endsWith("*")) parent.append(h("strong", undefined, part.slice(1, -1)));
    else if (part.length > 2 && part.startsWith("_") && part.endsWith("_")) parent.append(h("em", undefined, part.slice(1, -1)));
    else parent.append(document.createTextNode(part));
  }
}

const TICK_SVG = (double: boolean, blue: boolean): string =>
  `<svg class="tick${blue ? " blue" : ""}" viewBox="0 0 18 18" width="16" height="16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M1.5 9.5l3.5 3.5L12.5 5${
    double ? "M8.2 12.4l1.2 1.1L16.5 5" : ""
  }"/></svg>`;

function setTicks(meta: HTMLElement, status: string | null | undefined): void {
  const slot = meta.querySelector<HTMLElement>(".ticks");
  if (!slot) return;
  const s = (status ?? "delivered").toLowerCase();
  slot.innerHTML = s === "sending" ? "" : TICK_SVG(s !== "sent", s === "read" || s === "seen");
  slot.dataset.status = s;
}

function scrollDown(): void {
  chat.scrollTo({ top: chat.scrollHeight, behavior: "smooth" });
}

function setStatus(text: string, offline = false): void {
  statusEl.textContent = text;
  statusEl.classList.toggle("offline", offline);
}

// ---------- rendering ------------------------------------------------------------------------

function addSystem(text: string, extraClass = ""): HTMLElement {
  const note = h("div", `wa-system ${extraClass}`.trim(), text);
  insert(note);
  return note;
}

function insert(node: HTMLElement): void {
  if (typingEl && typingEl.parentElement === chat) chat.insertBefore(node, typingEl);
  else chat.append(node);
  scrollDown();
}

function metaRow(ts: string | number | null, mine: boolean, status: string | null): HTMLElement {
  const meta = h("span", "meta");
  meta.append(h("span", "time", clock(ts)));
  if (mine) {
    const ticks = h("span", "ticks");
    meta.append(ticks);
    setTicks(meta, status);
  }
  return meta;
}

function renderMessage(message: ChatMessage): HTMLElement {
  if (message.kind === "system") return h("div", "wa-system", message.text ?? "");
  const mine = message.direction === "in"; // "in" = from the citizen to Willay
  const row = h("div", `wa-row ${mine ? "mine" : "theirs"}`);
  const bubble = h("div", "bubble");
  row.append(bubble);

  switch (message.kind) {
    case "image": {
      if (message.image_url) {
        const link = h("a", "bubble-img");
        link.href = mediaUrl(message.image_url);
        link.target = "_blank";
        link.rel = "noopener";
        const img = h("img");
        img.src = mediaUrl(message.image_url);
        img.alt = message.text ?? "Imagen enviada por Willay";
        img.loading = "lazy";
        img.addEventListener("load", scrollDown);
        link.append(img);
        bubble.append(link);
      }
      if (message.text) formatInto(bubble.appendChild(h("p", "caption")), message.text);
      break;
    }
    case "location": {
      const card = h("a", "loc-card");
      card.href = `https://www.openstreetmap.org/?mlat=${message.lat}&mlon=${message.lon}#map=15/${message.lat}/${message.lon}`;
      card.target = "_blank";
      card.rel = "noopener";
      card.append(h("span", "loc-pin", "\u{1F4CD}"), h("strong", undefined, "Ubicación"));
      card.append(h("span", "loc-coords", `${Number(message.lat).toFixed(5)}, ${Number(message.lon).toFixed(5)}`));
      bubble.append(card);
      if (message.text) bubble.append(h("p", "caption", message.text));
      break;
    }
    case "audio": {
      const player = h("div", "voice");
      player.append(h("span", "voice-icon", "\u{1F3A4}"), h("span", "voice-wave"), h("span", "voice-label", "Nota de voz"));
      bubble.append(player);
      if (message.text) bubble.append(h("p", "transcript", `Transcripción: ${message.text}`));
      break;
    }
    default: {
      if (message.text) formatInto(bubble.appendChild(h("p")), message.text);
    }
  }
  bubble.append(metaRow(message.ts, mine, message.status));
  if (mine) userBubbles.push(bubble);

  if (message.kind === "buttons" && message.buttons?.length) {
    const group = h("div", "quick-replies");
    for (const button of message.buttons) {
      const b = h("button", "quick", button.title);
      b.type = "button";
      b.addEventListener("click", () => {
        for (const sibling of group.querySelectorAll("button")) {
          (sibling as HTMLButtonElement).disabled = true;
          sibling.classList.toggle("chosen", sibling === b);
        }
        void sendButton(button);
      });
      group.append(b);
    }
    const wrap = h("div", "wa-col");
    wrap.append(bubble, group);
    row.replaceChildren(wrap);
  }
  return row;
}

function showTyping(): void {
  typingUntil = Date.now() + TYPING_TIMEOUT_MS;
  if (!typingEl) {
    typingEl = h("div", "wa-row theirs typing");
    const bubble = h("div", "bubble");
    bubble.append(h("span", "dot"), h("span", "dot"), h("span", "dot"));
    typingEl.append(bubble);
    chat.append(typingEl);
  }
  setStatus("escribiendo…");
  scrollDown();
}

function hideTyping(): void {
  typingUntil = 0;
  typingEl?.remove();
  typingEl = null;
  if (!statusEl.classList.contains("offline")) setStatus("en línea");
}

// ---------- network --------------------------------------------------------------------------

async function request(path: string, init?: RequestInit): Promise<Response> {
  return fetch(apiUrl(path), init);
}

function storedSession(): string | null {
  try {
    return window.localStorage.getItem(SESSION_KEY);
  } catch {
    return null;
  }
}
function storeSession(id: string | null): void {
  try {
    if (id) window.localStorage.setItem(SESSION_KEY, id);
    else window.localStorage.removeItem(SESSION_KEY);
  } catch {
    // private mode: the conversation just will not survive a reload
  }
}

async function createSession(): Promise<boolean> {
  const params = new URLSearchParams(window.location.search);
  try {
    const response = await request("/webchat/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: params.get("name") }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = (await response.json()) as { session_id: string; display_name?: string };
    sessionId = data.session_id;
    storeSession(sessionId);
    lastSeq = 0;
    return true;
  } catch {
    return false;
  }
}

function showOffline(offline: boolean): void {
  offlineEl.hidden = !offline;
  if (offline) setStatus("sin conexión", true);
  else if (statusEl.classList.contains("offline")) {
    statusEl.classList.remove("offline");
    setStatus("en línea");
  }
}

async function ensureSession(): Promise<boolean> {
  if (sessionId) return true;
  const params = new URLSearchParams(window.location.search);
  if (params.has("new")) storeSession(null);
  const saved = storedSession();
  if (saved) {
    sessionId = saved;
    return true;
  }
  const ok = await createSession();
  if (!ok) showOffline(true);
  return ok;
}

function resetConversation(): void {
  window.clearTimeout(pollTimer);
  sessionId = null;
  lastSeq = 0;
  pending.length = 0;
  userBubbles.length = 0;
  hideTyping();
  chat.replaceChildren();
  storeSession(null);
  intro();
  void start();
}

function consume(messages: ChatMessage[]): void {
  let gotReply = false;
  for (const message of messages) {
    lastSeq = Math.max(lastSeq, message.seq);
    if (message.direction === "in") {
      // The citizen's own message echoed by the server: replace the optimistic bubble.
      const index = pending.findIndex((p) => p.kind === message.kind && (p.text === null || p.text === message.text));
      if (index >= 0) {
        pending[index].el.remove();
        pending.splice(index, 1);
      }
    } else {
      gotReply = true;
      for (const bubble of userBubbles) {
        const meta = bubble.querySelector<HTMLElement>(".meta");
        if (meta) setTicks(meta, "read");
      }
    }
    insert(renderMessage(message));
  }
  if (gotReply) hideTyping();
}

async function poll(): Promise<void> {
  if (polling || !sessionId) return;
  polling = true;
  try {
    const response = await request(`/webchat/sessions/${encodeURIComponent(sessionId)}/messages?after=${lastSeq}`);
    if (response.status === 404) {
      // Session unknown to the server (it restarted): start a fresh one.
      const hadSession = sessionId;
      sessionId = null;
      storeSession(null);
      if (hadSession && (await createSession())) {
        addSystem("La conversación se reinició porque el servidor se reinició.");
      } else showOffline(true);
      return;
    }
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = (await response.json()) as { messages: ChatMessage[] };
    failures = 0;
    showOffline(false);
    consume(data.messages);
  } catch {
    failures += 1;
    if (failures >= 2) showOffline(true);
  } finally {
    polling = false;
    if (typingUntil && Date.now() > typingUntil) {
      hideTyping();
      addSystem("Sin respuesta todavía. Si el mensaje no llega, revise el servidor.");
    }
    pollTimer = window.setTimeout(() => void poll(), failures ? 3000 : POLL_MS);
  }
}

async function start(): Promise<void> {
  if (!(await ensureSession())) {
    pollTimer = window.setTimeout(() => void start(), 5000);
    return;
  }
  showOffline(false);
  setStatus("en línea");
  void poll();
}

async function post(body: unknown): Promise<boolean> {
  if (!sessionId) return false;
  try {
    const response = await request(`/webchat/sessions/${encodeURIComponent(sessionId)}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return true;
  } catch {
    return false;
  }
}

function addPending(kind: string, text: string | null, build: () => HTMLElement): HTMLElement {
  const row = build();
  insert(row);
  pending.push({ kind, text, el: row });
  return row;
}

function pendingBubble(kind: ChatMessage["kind"], text: string | null, extra?: Partial<ChatMessage>): HTMLElement {
  return addPending(kind, text, () =>
    renderMessage({
      seq: -1,
      direction: "in",
      kind,
      text,
      buttons: null,
      image_url: null,
      lat: null,
      lon: null,
      ts: Date.now(),
      status: "sending",
      ...extra,
    }),
  );
}

function failBubble(row: HTMLElement, index: number): void {
  const bubble = row.querySelector(".bubble");
  bubble?.classList.add("failed");
  bubble?.append(h("span", "fail-note", "No se pudo enviar"));
  const at = pending.findIndex((p) => p.el === row);
  if (at >= 0) pending.splice(at, 1);
  void index;
}

async function sendText(text: string): Promise<void> {
  const row = pendingBubble("text", text);
  if (await post({ type: "text", text })) {
    showTyping();
    pollSoon();
  } else failBubble(row, 0);
}

async function sendButton(button: ChatButton): Promise<void> {
  const row = pendingBubble("text", button.title); // the server echoes the pressed button as an "in" text with its title
  if (await post({ type: "button", id: button.id })) {
    showTyping();
    pollSoon();
  } else failBubble(row, 0);
}

async function sendLocation(lat: number, lon: number): Promise<void> {
  const row = pendingBubble("location", null, { lat, lon });
  if (await post({ type: "location", lat, lon })) {
    showTyping();
    pollSoon();
  } else failBubble(row, 0);
}

function pollSoon(): void {
  window.clearTimeout(pollTimer);
  pollTimer = window.setTimeout(() => void poll(), 300);
}

// ---------- input bar ------------------------------------------------------------------------

function refreshSendMic(): void {
  const hasText = input.value.trim().length > 0;
  sendBtn.hidden = !hasText;
  micBtn.hidden = hasText;
}
input.addEventListener("input", refreshSendMic);
form.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  refreshSendMic();
  void sendText(text);
});

// location
locBtn.addEventListener("click", () => {
  sheet.hidden = !sheet.hidden;
});
$("wa-loc-cancel").addEventListener("click", () => (sheet.hidden = true));
$("wa-loc-demo").addEventListener("click", () => {
  sheet.hidden = true;
  void sendLocation(DEMO_POINT.lat, DEMO_POINT.lon);
});
$("wa-loc-real").addEventListener("click", () => {
  sheet.hidden = true;
  if (!navigator.geolocation) {
    addSystem("Este dispositivo no permite ubicación. Use el punto de demostración.", "warn");
    return;
  }
  setStatus("buscando ubicación…");
  navigator.geolocation.getCurrentPosition(
    (position) => {
      setStatus("en línea");
      void sendLocation(position.coords.latitude, position.coords.longitude);
    },
    () => {
      setStatus("en línea");
      addSystem("No se pudo obtener su ubicación. Pruebe «Usar punto de demostración».", "warn");
    },
    { enableHighAccuracy: true, timeout: 8000 },
  );
});

// voice notes (MediaRecorder): tap to start, tap send to upload, trash to cancel
let recorder: MediaRecorder | null = null;
let recChunks: Blob[] = [];
let recStream: MediaStream | null = null;
let recTimer: number | undefined;
let recStarted = 0;
let recCancelled = false;

function stopStream(): void {
  recStream?.getTracks().forEach((track) => track.stop());
  recStream = null;
  window.clearInterval(recTimer);
  recBar.hidden = true;
  form.hidden = false;
}

micBtn.addEventListener("click", async () => {
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
    addSystem("Este navegador no permite grabar audio.", "warn");
    return;
  }
  try {
    recStream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch {
    addSystem("No se pudo acceder al micrófono. Revise el permiso del navegador.", "warn");
    return;
  }
  recChunks = [];
  recCancelled = false;
  recorder = new MediaRecorder(recStream);
  recorder.addEventListener("dataavailable", (event) => {
    if (event.data.size) recChunks.push(event.data);
  });
  recorder.addEventListener("stop", () => {
    const type = recorder?.mimeType || "audio/webm";
    stopStream();
    if (recCancelled || !recChunks.length) return;
    void uploadAudio(new Blob(recChunks, { type }));
  });
  recorder.start();
  recStarted = Date.now();
  form.hidden = true;
  recBar.hidden = false;
  recTime.textContent = "0:00";
  recTimer = window.setInterval(() => {
    const seconds = Math.floor((Date.now() - recStarted) / 1000);
    recTime.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
    if (seconds >= 60) recorder?.stop(); // keep notes short
  }, 250);
});
$("wa-rec-send").addEventListener("click", () => recorder?.state === "recording" && recorder.stop());
$("wa-rec-cancel").addEventListener("click", () => {
  recCancelled = true;
  if (recorder?.state === "recording") recorder.stop();
  else stopStream();
});

async function uploadAudio(blob: Blob): Promise<void> {
  if (!sessionId) return;
  const row = pendingBubble("audio", null);
  const extension = blob.type.includes("mp4") ? "m4a" : blob.type.includes("ogg") ? "ogg" : "webm";
  const body = new FormData();
  body.append("file", blob, `nota-de-voz.${extension}`);
  showTyping();
  try {
    const response = await request(`/webchat/sessions/${encodeURIComponent(sessionId)}/audio`, { method: "POST", body });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    pollSoon();
  } catch {
    hideTyping();
    failBubble(row, 0);
  }
}

// ---------- menu + boot ----------------------------------------------------------------------

const menuBtn = $<HTMLButtonElement>("wa-menu-btn");
const menuPop = $<HTMLElement>("wa-menu-pop");
menuBtn.addEventListener("click", () => {
  menuPop.hidden = !menuPop.hidden;
  menuBtn.setAttribute("aria-expanded", String(!menuPop.hidden));
});
$("wa-new").addEventListener("click", () => {
  menuPop.hidden = true;
  resetConversation();
});
$("wa-retry").addEventListener("click", () => {
  failures = 0;
  window.clearTimeout(pollTimer);
  void start();
});

function intro(): void {
  addSystem("HOY", "day");
  addSystem(
    "Conversación de demostración con hogares SINTÉTICOS. Willay complementa, no reemplaza, las alertas oficiales de SENAMHI, INDECI y ENFEN.",
    "notice",
  );
}

intro();
refreshSendMic();
void start();
