import { el } from "./panel";

const STORAGE_KEY = "willay.intro.seen";

const CARDS: { n: string; title: string; text: string; tag?: string }[] = [
  {
    n: "1",
    title: "Lee las fuentes oficiales",
    text: "Willay lee los comunicados de ENFEN, los avisos de SENAMHI y los datos de SIGRID-CENEPRED. No inventa alertas: parte de lo que ya publican las instituciones.",
    tag: "ENFEN · SENAMHI · CENEPRED",
  },
  {
    n: "2",
    title: "Los agentes analizan y proponen",
    text: "Agentes de IA de Meta cruzan esas fuentes con los relaves mineros de las cuencas del Rímac y Lurín: ¿qué sitio, qué poblados aguas abajo, qué tan urgente?",
    tag: "Muse Wonder · Muse Spark · SAM 3.1",
  },
  {
    n: "3",
    title: "Las reglas deciden y una persona confirma",
    text: "El nivel de alerta lo calcula código determinista, no la IA. Los agentes solo proponen: nada sale sin que un operador humano confirme.",
    tag: "Reglas + operador humano",
  },
  {
    n: "4",
    title: "El aviso llega a los hogares por WhatsApp",
    text: "Cada hogar recibe el mensaje en su celular y responde con un toque: «estoy a salvo» o «necesito ayuda». También puede mandar ubicación o una nota de voz.",
    tag: "WhatsApp · Voice Transcribe · Muse Image",
  },
  {
    n: "5",
    title: "El operador ve quién necesita ayuda",
    text: "En el mapa y en la pestaña Alertas se ve quién está a salvo y quién pide ayuda. Cada pedido abre un caso para atender.",
    tag: "Mapa + casos abiertos",
  },
];

function seen(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === "1";
  } catch {
    return false;
  }
}

function remember(): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, "1");
  } catch {
    // private mode: the intro will simply show again
  }
}

/** First-load "¿Cómo funciona?" overlay; re-openable with the "?" button in the top bar. */
export function initIntro(onStartDemo: () => void): void {
  const openButton = document.getElementById("help-open") as HTMLButtonElement;
  let overlay: HTMLElement | null = null;

  const close = (): void => {
    overlay?.remove();
    overlay = null;
    remember();
    document.removeEventListener("keydown", onKey);
  };
  const onKey = (event: KeyboardEvent): void => {
    if (event.key === "Escape") close();
  };

  const open = (): void => {
    if (overlay) return;
    overlay = el("div", "intro-overlay");
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-labelledby", "intro-title");
    const panel = el("div", "intro-panel");
    const head = el("div", "intro-head");
    const title = el("h2", undefined, "¿Cómo funciona Willay?");
    title.id = "intro-title";
    head.append(title, el("p", "muted", "De una señal oficial a una familia a salvo, en cinco pasos."));
    const grid = el("div", "intro-grid");
    for (const card of CARDS) {
      const item = el("article", "intro-card");
      item.append(el("span", "intro-n", card.n), el("h3", undefined, card.title), el("p", undefined, card.text));
      if (card.tag) item.append(el("span", "intro-tag", card.tag));
      grid.append(item);
    }
    const sim = el(
      "p",
      "intro-sim",
      "Todo lo que ve aquí en modo demostración es SIMULADO: hogares sintéticos, clips y avisos de ejemplo. No se envía nada a teléfonos reales. Willay complementa, no reemplaza, las alertas oficiales.",
    );
    const actions = el("div", "intro-actions");
    const demo = el("button", "primary cta-btn", "▶ Ver la demostración");
    demo.type = "button";
    demo.addEventListener("click", () => {
      close();
      onStartDemo();
    });
    const ok = el("button", undefined, "Entendido, explorar");
    ok.type = "button";
    ok.addEventListener("click", close);
    actions.append(demo, ok);
    panel.append(head, grid, sim, actions);
    overlay.append(panel);
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) close();
    });
    document.body.append(overlay);
    document.addEventListener("keydown", onKey);
    ok.focus();
  };

  openButton.addEventListener("click", open);
  if (!seen()) open();
}
