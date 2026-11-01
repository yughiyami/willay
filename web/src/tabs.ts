/** Tab strip of the console: one visible panel at a time, keyboard accessible. */
export function initTabs(onChange?: (tab: string) => void): { select(tab: string): void; current(): string } {
  const buttons = [...document.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
  const panels = [...document.querySelectorAll<HTMLElement>('[role="tabpanel"]')];
  let current = buttons.find((b) => b.getAttribute("aria-selected") === "true")?.dataset.tab ?? "agentes";

  const select = (tab: string): void => {
    current = tab;
    for (const button of buttons) {
      const active = button.dataset.tab === tab;
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
    }
    for (const panel of panels) panel.hidden = panel.dataset.panel !== tab;
    onChange?.(tab);
  };

  buttons.forEach((button, index) => {
    button.addEventListener("click", () => select(button.dataset.tab as string));
    button.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      const next = buttons[(index + (event.key === "ArrowRight" ? 1 : buttons.length - 1)) % buttons.length];
      next.focus();
      select(next.dataset.tab as string);
    });
  });
  select(current);
  return { select, current: () => current };
}
