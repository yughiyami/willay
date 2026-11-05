import { el } from "./panel";

export type ModelKind = "spark" | "wonder" | "sam" | "image" | "voice" | "chat" | "human" | "rules";

export interface WorkflowStep {
  key: string;
  label: string;
  title: string;
  what: string;
  who: { kind: ModelKind; name: string }[];
  note: string;
}

/** The scripted scenario, in plain Spanish. `key` matches the `step` events emitted by the backend demo. */
export const WORKFLOW_STEPS: WorkflowStep[] = [
  {
    key: "enfen",
    label: "ENFEN extraordinario",
    title: "1 · ENFEN: Niño costero extraordinario",
    what: "Willay lee el comunicado oficial del ENFEN y extrae la magnitud esperada y la anomalía de lluvia. Cada dato se verifica contra el texto del documento.",
    who: [
      { kind: "spark", name: "Muse Spark (lee el comunicado)" },
      { kind: "rules", name: "Reglas del sistema" },
    ],
    note: "Fuente real: comunicado ENFEN. En la demostración el escenario se fija para mostrar el caso extremo.",
  },
  {
    key: "senamhi",
    label: "SENAMHI naranja",
    title: "2 · SENAMHI: aviso naranja de lluvias",
    what: "El Analista de peligro cruza el aviso del SENAMHI y el pronóstico de lluvia a 72 h con los relaves de las cuencas del Rímac y Lurín, y propone qué sitios vigilar.",
    who: [
      { kind: "wonder", name: "Muse Wonder (planifica)" },
      { kind: "spark", name: "Muse Spark (Analista de peligro)" },
    ],
    note: "SIMULADO en la demostración: el aviso naranja forma parte del escenario.",
  },
  {
    key: "watcher",
    label: "Vigía: el río sube",
    title: "3 · Vigía: el río sube",
    what: "Un vecino sube un clip corto del río con un poste marcador. SAM 3.1 separa el agua y el poste en el video; la subida en centímetros se calcula con geometría, no con un modelo.",
    who: [
      { kind: "sam", name: "SAM 3.1 (segmenta agua y poste)" },
      { kind: "voice", name: "Voice Transcribe (si llega en nota de voz)" },
    ],
    note: "SIMULADO: se usa un clip de demostración, sin personas.",
  },
  {
    key: "operator",
    label: "Operador confirma",
    title: "4 · El operador confirma",
    what: "Las reglas de dominio (código, no IA) calculan el nivel de alerta. Los agentes solo proponen: una persona revisa la propuesta de evacuación, escribe su id y confirma.",
    who: [
      { kind: "rules", name: "Reglas: deciden el nivel" },
      { kind: "human", name: "Operador humano: confirma" },
    ],
    note: "Nada se envía sin confirmación humana. Con «confirmar solo» el clic se simula y queda registrado como simulado.",
  },
  {
    key: "dispatch",
    label: "Envío a hogares",
    title: "5 · Envío a los hogares",
    what: "El Despachador y el Comunicador preparan el mensaje y la ficha ilustrada. El aviso llega al hogar por WhatsApp (y una llamada) con botones para responder de un toque.",
    who: [
      { kind: "spark", name: "Muse Spark (Despachador, Comunicador)" },
      { kind: "image", name: "Muse Image (ficha)" },
      { kind: "chat", name: "WhatsApp / chat web" },
    ],
    note: "Hogares SINTÉTICOS: no se envía nada a teléfonos reales.",
  },
  {
    key: "household_reply",
    label: "Hogar responde",
    title: "6 · El hogar responde",
    what: "La familia responde con un toque: «1 estoy a salvo» o «2 necesito ayuda». También puede compartir su ubicación o mandar una nota de voz.",
    who: [
      { kind: "chat", name: "WhatsApp / chat web" },
      { kind: "voice", name: "Voice Transcribe (notas de voz)" },
    ],
    note: "En la demostración la respuesta es simulada. Abra «Abrir chat del ciudadano» para responder usted mismo.",
  },
  {
    key: "done",
    label: "Caso abierto",
    title: "7 · Caso abierto",
    what: "El operador ve en el mapa y en la pestaña Alertas quién está a salvo y quién necesita ayuda. Cada «necesito ayuda» abre un caso para atender.",
    who: [{ kind: "human", name: "Operador: atiende el caso" }],
    note: "Es el cierre del ciclo: de la señal oficial a una persona atendida.",
  },
];

const STATE_LABEL = { todo: "pendiente", current: "en curso", done: "hecho" } as const;

export interface WorkflowBar {
  /** `-2` idle, `-1` started (nothing done yet), `n` step n in progress, `STEPS.length` all done. */
  setProgress(current: number): void;
}

export function initWorkflowBar(): WorkflowBar {
  const list = document.getElementById("demo-steps") as HTMLOListElement;
  const detail = document.getElementById("step-detail") as HTMLElement;
  let progress = -2;
  let pinned: number | null = null;

  const renderDetail = (): void => {
    const index = pinned ?? (progress >= 0 && progress < WORKFLOW_STEPS.length ? progress : null);
    detail.hidden = index === null; // idle or finished: the header line is enough, keep the console tall
    if (index === null) {
      detail.replaceChildren();
      return;
    }
    const step = WORKFLOW_STEPS[index];
    const chips = el("div", "step-who");
    for (const model of step.who) chips.append(el("span", `model-chip wm-${model.kind}`, model.name));
    detail.replaceChildren(
      el("h4", undefined, step.title),
      el("p", "step-what", step.what),
      chips,
      el("p", "step-note", step.note),
    );
  };

  const render = (): void => {
    list.replaceChildren(
      ...WORKFLOW_STEPS.map((step, index) => {
        const state = index < progress ? "done" : index === progress ? "current" : "todo";
        const item = el("li", "demo-step-item");
        const button = el("button", `demo-step ${state}${pinned === index ? " pinned" : ""}`);
        button.type = "button";
        button.setAttribute("aria-pressed", String(pinned === index));
        button.title = "Pulse para ver qué ocurre en este paso";
        if (state === "current") button.setAttribute("aria-current", "step");
        const head = el("span", "demo-step-head");
        head.append(el("span", "demo-n", state === "done" ? "✓" : String(index + 1)), el("span", "demo-label", step.label));
        const tags = el("span", "demo-step-tags");
        for (const model of step.who) tags.append(el("span", `model-dot wm-${model.kind}`));
        button.append(head, el("span", "demo-state", STATE_LABEL[state]), tags);
        button.addEventListener("click", () => {
          pinned = pinned === index ? null : index;
          render();
        });
        item.append(button);
        return item;
      }),
    );
    renderDetail();
  };

  render();
  return {
    setProgress(current) {
      progress = current;
      pinned = null; // follow the running step again
      render();
    },
  };
}
