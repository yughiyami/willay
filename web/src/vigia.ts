import { analyseClip, analyseDemoClip } from "./consoleApi";
import type { WatchResult } from "./consoleTypes";
import { el } from "./panel";

interface VigiaOptions {
  onClipRegistered(clipId: string): void;
  /** The measured rise can be used as the water-rise input of the scenario (the operator decides). */
  onMeasured(riseCm: number | null): void;
}

function sparkline(series: WatchResult["series"]): SVGSVGElement {
  const ns = "http://www.w3.org/2000/svg";
  const width = 360;
  const height = 70;
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("class", "spark");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Subida del agua por cuadro, en centímetros");
  const points = series.filter((s) => s.valid && s.rise_cm !== null);
  if (points.length < 2) return svg;
  const max = Math.max(60, ...points.map((p) => p.rise_cm as number));
  const last = Math.max(...series.map((s) => s.frame), 1);
  const x = (frame: number): number => 6 + (frame / last) * (width - 12);
  const y = (cm: number): number => height - 8 - (Math.max(0, cm) / max) * (height - 16);
  const threshold = document.createElementNS(ns, "line");
  threshold.setAttribute("x1", "6");
  threshold.setAttribute("x2", String(width - 6));
  threshold.setAttribute("y1", String(y(30)));
  threshold.setAttribute("y2", String(y(30)));
  threshold.setAttribute("class", "spark-threshold");
  const path = document.createElementNS(ns, "polyline");
  path.setAttribute("points", points.map((p) => `${x(p.frame).toFixed(1)},${y(p.rise_cm as number).toFixed(1)}`).join(" "));
  path.setAttribute("class", "spark-line");
  const label = document.createElementNS(ns, "text");
  label.setAttribute("x", "8");
  label.setAttribute("y", String(y(30) - 3));
  label.setAttribute("class", "spark-label");
  label.textContent = "30 cm (umbral de las reglas)";
  svg.append(threshold, path, label);
  return svg;
}

export function renderWatch(container: HTMLElement, result: WatchResult): void {
  const children: HTMLElement[] = [];
  const rise = result.rise_cm;
  const headline = el("p", "score", rise === null ? "No se pudo medir la subida" : `Subida medida: ${rise.toFixed(1)} cm`);
  children.push(headline);
  children.push(
    el(
      "p",
      "muted",
      `Confianza ${result.confidence.toFixed(2)} · ${result.valid_frames}/${result.total_frames} cuadros válidos` +
        (result.analyzer ? ` · ${result.analyzer}` : "") +
        (result.estimated_cost_usd !== null && result.estimated_cost_usd > 0 ? ` · costo estimado $${result.estimated_cost_usd.toFixed(4)}` : ""),
    ),
  );
  if (result.reasons.length) children.push(el("p", "banner", `Calidad: ${result.reasons.join(", ")}`));
  if (result.synthetic_clip) children.push(el("p", "banner", "Clip SINTÉTICO de demostración (escena falsa, sin personas): no mide ningún río real."));
  const strip = el("div", "overlay-strip");
  for (const overlay of result.overlays) {
    const img = el("img", "overlay-img");
    img.src = `data:image/png;base64,${overlay.png_base64}`;
    img.alt = `Cuadro ${overlay.frame} con máscaras de agua (azul), poste (rojo) y línea de agua (verde)`;
    strip.append(img);
  }
  children.push(strip);
  children.push(sparkline(result.series) as unknown as HTMLElement);
  children.push(el("p", "muted legend", result.note));
  container.replaceChildren(...children);
}

export function initVigia(options: VigiaOptions): void {
  const form = document.getElementById("watch-form") as HTMLFormElement;
  const file = document.getElementById("watch-file") as HTMLInputElement;
  const marker = document.getElementById("watch-marker") as HTMLInputElement;
  const noPeople = document.getElementById("watch-nopeople") as HTMLInputElement;
  const register = document.getElementById("watch-register") as HTMLInputElement;
  const send = document.getElementById("watch-send") as HTMLButtonElement;
  const demo = document.getElementById("watch-demo") as HTMLButtonElement;
  const status = document.getElementById("watch-status") as HTMLElement;
  const out = document.getElementById("watch-result") as HTMLElement;

  const show = (result: WatchResult): void => {
    renderWatch(out, result);
    options.onMeasured(result.rise_cm);
    if (result.clip_id) options.onClipRegistered(result.clip_id);
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const clip = file.files?.[0];
    if (!clip) {
      status.textContent = "Elija un clip primero.";
      return;
    }
    if (!noPeople.checked) {
      status.textContent = "Confirme que el clip no muestra personas: se envía a un modelo de un tercero.";
      return;
    }
    send.disabled = true;
    status.textContent = "SAM 3.1 está segmentando el clip (dos consultas: agua y poste)…";
    analyseClip(clip, Number(marker.value), register.checked)
      .then((result) => {
        status.textContent = "";
        show(result);
      })
      .catch((error: Error) => {
        status.textContent = `No se pudo analizar: ${error.message}`;
      })
      .finally(() => {
        send.disabled = false;
      });
  });

  demo.addEventListener("click", () => {
    demo.disabled = true;
    status.textContent = "Analizando el clip de demostración (sin red)…";
    analyseDemoClip()
      .then((result) => {
        status.textContent = "";
        show(result);
      })
      .catch((error: Error) => {
        status.textContent = `No se pudo analizar: ${error.message}`;
      })
      .finally(() => {
        demo.disabled = false;
      });
  });
}
