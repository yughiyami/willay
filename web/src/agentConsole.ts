import { MicRecorder, recordingToWav } from "./audio";
import { artifactUrl, fetchTrace, followRun, startAgentRun, startAgentVoiceRun } from "./consoleApi";
import type { RunResult, ScenarioInput, TraceEvent, TraceSnapshot } from "./consoleTypes";
import { el } from "./panel";
import { TraceView } from "./trace";

interface AgentConsoleOptions {
  /** Every trace event of every followed run (the demo stepper and the proposal list listen here). */
  onEvent(event: TraceEvent): void;
  /** A run ended: execute its map actions, refresh the alert tab. */
  onRunEnd(snapshot: TraceSnapshot): void;
}

export interface AgentConsole {
  /** Follow a run started elsewhere (the demo) in the same timeline and chat. */
  followExternalRun(runId: string, label: string): Promise<TraceSnapshot>;
  setClipId(clipId: string | null): void;
}

function scenarioFromForm(): ScenarioInput | null {
  const enfen = (document.getElementById("sc-enfen") as HTMLSelectElement).value;
  const senamhi = (document.getElementById("sc-senamhi") as HTMLSelectElement).value;
  const f72 = (document.getElementById("sc-f72") as HTMLInputElement).value;
  const o24 = (document.getElementById("sc-o24") as HTMLInputElement).value;
  const scenario: ScenarioInput = {};
  if (enfen) scenario.enfen_magnitude = enfen;
  if (senamhi) scenario.senamhi_level = senamhi;
  if (f72 !== "") scenario.forecast_rain_72h_mm = Number(f72);
  if (o24 !== "") scenario.observed_rain_24h_mm = Number(o24);
  return Object.keys(scenario).length ? scenario : null;
}

function bubble(role: "user" | "agent" | "system", text: string): HTMLElement {
  const node = el("div", `msg msg-${role}`);
  for (const line of text.split("\n")) node.append(el("p", undefined, line));
  return node;
}

function resultExtras(runId: string, snapshot: TraceSnapshot): HTMLElement[] {
  const extras: HTMLElement[] = [];
  const result = snapshot.result as RunResult | null;
  if (!result) return extras;
  if (result.flags?.length) extras.push(el("p", "msg-flags", `Salvaguardas activadas: ${result.flags.join(", ")}`));
  if (snapshot.artifacts.includes("card.png")) {
    const img = el("img", "msg-img");
    img.src = artifactUrl(runId, "card.png");
    img.alt = "Ficha de evacuación generada por Muse Image (requiere revisión humana)";
    extras.push(img, el("p", "msg-flags", "Ficha de Muse Image: revisar el texto contra el texto previsto antes de usarla."));
  }
  const overlays = snapshot.artifacts.filter((a) => a.startsWith("overlay_"));
  if (overlays.length) {
    const strip = el("div", "overlay-strip");
    for (const name of overlays) {
      const img = el("img", "overlay-img");
      img.src = artifactUrl(runId, name);
      img.alt = "Cuadro del clip con la máscara del agua (azul), el poste (rojo) y la línea de agua (verde)";
      strip.append(img);
    }
    extras.push(strip);
  }
  const message = result.artifacts_info?.["message"] as { text?: string; draft_pending_confirmation?: boolean } | undefined;
  if (message?.text) {
    const box = el("blockquote", "msg-quote");
    for (const line of message.text.split("\n")) box.append(el("p", undefined, line));
    extras.push(el("p", "msg-flags", message.draft_pending_confirmation ? "Mensaje para hogares (borrador, vale solo si el operador confirma):" : "Mensaje para hogares:"), box);
  }
  return extras;
}

export function initAgentConsole(options: AgentConsoleOptions): AgentConsole {
  const log = document.getElementById("chat-log") as HTMLElement;
  const form = document.getElementById("agent-form") as HTMLFormElement;
  const input = document.getElementById("agent-input") as HTMLInputElement;
  const sendButton = document.getElementById("agent-send") as HTMLButtonElement;
  const micButton = document.getElementById("agent-mic") as HTMLButtonElement;
  const status = document.getElementById("agent-status") as HTMLElement;
  const view = new TraceView(document.getElementById("trace-list") as HTMLElement, document.getElementById("trace-totals") as HTMLElement);
  const recorder = new MicRecorder();
  let clipId: string | null = null;
  let busy = false;
  let external = false; // following the demo: planner answers also go to the chat

  const setBusy = (value: boolean, message = ""): void => {
    busy = value;
    sendButton.disabled = value;
    micButton.disabled = value && !recorder.recording;
    status.textContent = message;
  };

  const append = (node: HTMLElement): void => {
    log.append(node);
    log.scrollTop = log.scrollHeight;
  };

  const follow = (runId: string): Promise<TraceSnapshot> =>
    new Promise((resolve) => {
      view.clear();
      followRun(
        runId,
        (event) => {
          view.add(event);
          options.onEvent(event);
          const said = event.detail["text"];
          if (external && event.kind === "agent_end" && event.agent === "planner" && typeof said === "string") append(bubble("agent", said));
        },
        () => {
          void fetchTrace(runId).then((snapshot) => {
            options.onRunEnd(snapshot);
            resolve(snapshot);
          });
        },
      );
    });

  const showAnswer = (runId: string, snapshot: TraceSnapshot): void => {
    const result = snapshot.result;
    if (snapshot.status === "failed" || !result) {
      append(bubble("system", `La corrida falló: ${result?.error ?? "sin detalle"}`));
      return;
    }
    const node = bubble("agent", result.answer_es || result.outcome || "Corrida terminada.");
    for (const extra of resultExtras(runId, snapshot)) node.append(extra);
    append(node);
  };

  const run = async (starter: () => Promise<{ run_id: string; transcript?: string }>, userText: string | null): Promise<void> => {
    setBusy(true, "Los agentes están trabajando…");
    try {
      const started = await starter();
      if (userText || started.transcript) append(bubble("user", userText ?? `🎤 ${started.transcript}`));
      const snapshot = await follow(started.run_id);
      showAnswer(started.run_id, snapshot);
      setBusy(false);
    } catch (error) {
      append(bubble("system", `No se pudo consultar a los agentes: ${(error as Error).message}`));
      setBusy(false);
    }
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || busy) return;
    input.value = "";
    void run(() => startAgentRun(text, scenarioFromForm(), clipId), text);
  });

  micButton.addEventListener("click", () => {
    if (recorder.recording) {
      micButton.setAttribute("aria-pressed", "false");
      micButton.textContent = "Hablar";
      setBusy(true, "Transcribiendo…");
      void recorder
        .stop()
        .then(recordingToWav)
        .then((wav) => run(() => startAgentVoiceRun(wav), null))
        .catch((error) => {
          append(bubble("system", `No se pudo usar el micrófono: ${(error as Error).message}`));
          setBusy(false);
        });
      return;
    }
    if (busy) return;
    void recorder
      .start()
      .then(() => {
        micButton.setAttribute("aria-pressed", "true");
        micButton.textContent = "Detener";
        status.textContent = "Escuchando… pulse Detener al terminar (máx. 30 s).";
      })
      .catch((error) => {
        status.textContent = `Micrófono no disponible: ${(error as Error).message}`;
      });
  });

  append(
    bubble(
      "system",
      "Pregunte al Planificador (muse-wonder). Delega en cuatro especialistas (muse-spark): Analista de peligro, Vigía (SAM 3.1), Despachador y Comunicador. O pulse «Modo demostración» arriba.",
    ),
  );

  return {
    setClipId(id) {
      clipId = id;
    },
    async followExternalRun(runId, label) {
      append(bubble("system", label));
      setBusy(true, "Demostración en curso…");
      external = true;
      const snapshot = await follow(runId);
      external = false;
      const result = snapshot.result;
      if (snapshot.status === "failed") append(bubble("system", `La demostración se detuvo: ${result?.error ?? "sin detalle"}`));
      else append(bubble("system", `Demostración terminada (${String(result?.outcome ?? "ok")}). Costo estimado: $${snapshot.totals.cost_usd.toFixed(4)}.`));
      setBusy(false);
      return snapshot;
    },
  };
}
