import { fetchAgentConfig, fetchHealth } from "./consoleApi";

/** Status chips in the top bar: data mode, Meta availability and spend, telephony mode. */
export async function refreshChips(): Promise<{ live: boolean; offlineDemo: boolean }> {
  const data = document.getElementById("chip-data") as HTMLElement;
  const meta = document.getElementById("chip-meta") as HTMLElement;
  const calls = document.getElementById("chip-calls") as HTMLElement;
  let live = false;
  try {
    const health = await fetchHealth();
    data.textContent = health.synthetic_data ? "datos SINTÉTICOS" : "datos reales abiertos";
    data.className = `chip ${health.synthetic_data ? "chip-warn" : "chip-ok"}`;
    calls.textContent = health.calls_mode.startsWith("simulated") ? "llamadas simuladas" : `llamadas: ${health.calls_mode.split(" ")[0]}`;
    calls.title = health.calls_mode;
    calls.className = `chip ${health.calls_mode.startsWith("simulated") ? "chip-ok" : "chip-warn"}`;
    const chat = document.getElementById("chip-chat");
    if (chat) {
      const webchat = health.webchat_mode ?? "no disponible";
      const telegram = health.telegram_mode ?? "no disponible";
      const chatLive = webchat.startsWith("live");
      chat.textContent = `chat web: ${chatLive ? "activo" : "apagado"} · Telegram: ${telegram.startsWith("disabled") ? "apagado" : telegram.split(" ")[0]}`;
      chat.title = `Chat web: ${webchat}\nTelegram: ${telegram}\nMensajería: ${health.messaging_mode ?? "sin datos"}`;
      chat.className = `chip ${chatLive ? "chip-ok" : ""}`;
    }
  } catch {
    data.textContent = "sin conexión al servidor";
    data.className = "chip chip-warn";
  }
  try {
    const config = await fetchAgentConfig();
    live = config.live;
    const spend = config.budget ? ` · $${config.budget.spent_usd.toFixed(2)} de $${config.budget.limit_usd.toFixed(2)}` : "";
    meta.textContent = config.live ? `Meta en vivo${spend}` : "Meta apagado (modo grabado)";
    meta.title = `Planificador ${config.planner_model} (respaldo ${config.planner_fallback_model}); especialistas ${config.specialist_model}`;
    meta.className = `chip ${config.live ? "chip-ok" : ""}`;
  } catch {
    meta.textContent = "Meta: sin datos";
  }
  const liveOption = document.querySelector<HTMLOptionElement>('#demo-mode option[value="live"]');
  if (liveOption) liveOption.disabled = !live;
  return { live, offlineDemo: true };
}
