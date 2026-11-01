import { fetchSiteDetail } from "./api";
import { type CardReply, generateCard, recordedCardUrl } from "./consoleApi";
import { el } from "./panel";
import type { Site } from "./types";

const HOUSEHOLD_TIERS = ["PREPARATION", "ATTENTION", "EVACUATION"];

/** "Fichas": one evacuation card for a synthetic household at the nearest settlement downstream of a site. */
export function initCards(sites: Site[]): void {
  const siteSelect = document.getElementById("card-site") as HTMLSelectElement;
  const tierSelect = document.getElementById("card-tier") as HTMLSelectElement;
  const evaluated = document.getElementById("card-evaluated") as HTMLElement;
  const generate = document.getElementById("card-generate") as HTMLButtonElement;
  const recorded = document.getElementById("card-recorded") as HTMLButtonElement;
  const status = document.getElementById("card-status") as HTMLElement;
  const out = document.getElementById("card-result") as HTMLElement;

  for (const site of sites) {
    const option = el("option", undefined, `#${site.rank} ${site.name}`);
    option.value = site.id;
    siteSelect.append(option);
  }

  const nearest = (siteId: string) => {
    const site = sites.find((s) => s.id === siteId);
    const reached = site?.exposure?.settlements ?? [];
    return reached.length ? reached.reduce((a, b) => (a.along_path_km <= b.along_path_km ? a : b)) : null;
  };

  const syncTier = async (): Promise<void> => {
    try {
      const detail = await fetchSiteDetail(siteSelect.value);
      evaluated.textContent = `Nivel que devuelven las reglas ahora: ${detail.alert.tier}${HOUSEHOLD_TIERS.includes(detail.alert.tier) ? "" : " (elija el nivel de la ficha a mano)"}`;
      if (HOUSEHOLD_TIERS.includes(detail.alert.tier)) tierSelect.value = detail.alert.tier;
    } catch {
      evaluated.textContent = "";
    }
  };
  siteSelect.addEventListener("change", () => void syncTier());
  void syncTier();

  const show = (src: string, intended: Record<string, string> | null, note: string): void => {
    const img = el("img", "card-img");
    img.src = src;
    img.alt = "Ficha de evacuación ilustrada";
    const children: HTMLElement[] = [img, el("p", "banner", note)];
    if (intended) {
      const list = el("ul", "reasons");
      for (const line of Object.values(intended)) list.append(el("li", undefined, line));
      children.push(el("h3", undefined, "Texto previsto (el que debe leerse en la ficha)"), list);
    }
    out.replaceChildren(...children);
  };

  generate.addEventListener("click", () => {
    const target = nearest(siteSelect.value);
    if (!target) {
      status.textContent = "Este relave no tiene poblados aguas abajo.";
      return;
    }
    generate.disabled = true;
    status.textContent = "Muse Image está dibujando la ficha (≈ 15 s, US$0,01)…";
    generateCard(target.settlement_name, target.distance_km, target.settlement_id, tierSelect.value)
      .then((card: CardReply) => {
        status.textContent = "";
        show(
          `data:image/png;base64,${card.image_png_base64}`,
          card.intended_text,
          "Hogar sintético. Los modelos de imagen pueden escribir mal el texto: una persona debe revisar la ficha contra el texto previsto.",
        );
      })
      .catch((error: Error) => {
        status.textContent = `No se pudo generar: ${error.message}. Sin Meta en vivo, use la ficha grabada.`;
      })
      .finally(() => {
        generate.disabled = false;
      });
  });

  recorded.addEventListener("click", () => {
    status.textContent = "";
    show(recordedCardUrl(), null, "Ficha grabada en la corrida en vivo de la demostración (Muse Image). Requiere revisión humana del texto.");
  });
}
