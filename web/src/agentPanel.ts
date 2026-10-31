import { askAgentText, askAgentVoice } from "./api";
import { MicRecorder, recordingToWav } from "./audio";
import type { AgentReply } from "./types";

interface AgentPanelOptions {
  onReply(reply: AgentReply): void;
}

/** Text box + microphone button for the Spanish operator assistant. */
export function initAgentPanel(options: AgentPanelOptions): void {
  const form = document.getElementById("agent-form") as HTMLFormElement;
  const input = document.getElementById("agent-input") as HTMLInputElement;
  const sendButton = document.getElementById("agent-send") as HTMLButtonElement;
  const micButton = document.getElementById("agent-mic") as HTMLButtonElement;
  const statusEl = document.getElementById("agent-status") as HTMLElement;
  const transcriptEl = document.getElementById("agent-transcript") as HTMLElement;
  const answerEl = document.getElementById("agent-answer") as HTMLElement;

  const recorder = new MicRecorder();
  let conversationId: string | null = null;
  let busy = false;

  const setBusy = (value: boolean, message = ""): void => {
    busy = value;
    sendButton.disabled = value;
    micButton.disabled = value && !recorder.recording;
    statusEl.textContent = message;
  };

  const show = (reply: AgentReply): void => {
    conversationId = reply.response_id || conversationId;
    transcriptEl.textContent = reply.transcript ? `Tu pregunta: ${reply.transcript}` : "";
    answerEl.textContent = reply.answer_es;
    options.onReply(reply);
  };

  const fail = (error: unknown): void => {
    answerEl.textContent = "";
    statusEl.textContent = `No se pudo consultar al asistente: ${(error as Error).message}`;
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text || busy) return;
    setBusy(true, "Consultando...");
    askAgentText(text, conversationId)
      .then((reply) => {
        input.value = "";
        show(reply);
        setBusy(false);
      })
      .catch((error) => {
        fail(error);
        setBusy(false, statusEl.textContent ?? "");
      });
  });

  micButton.addEventListener("click", () => {
    if (recorder.recording) {
      micButton.setAttribute("aria-pressed", "false");
      micButton.textContent = "Talk";
      setBusy(true, "Transcribiendo...");
      recorder
        .stop()
        .then(recordingToWav)
        .then((wav) => askAgentVoice(wav, conversationId))
        .then((reply) => {
          show(reply);
          setBusy(false);
        })
        .catch((error) => {
          fail(error);
          setBusy(false, statusEl.textContent ?? "");
        });
      return;
    }
    if (busy) return;
    recorder
      .start()
      .then(() => {
        micButton.setAttribute("aria-pressed", "true");
        micButton.textContent = "Stop";
        statusEl.textContent = "Escuchando... pulsa Stop al terminar (máx. 30 s).";
      })
      .catch((error) => fail(error));
  });
}
