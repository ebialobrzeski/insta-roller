// What every screen shares: the job list (polled once, for all), the theme, the top bar's context, the player.
import {$, h, api, icon, store, saveButton} from "./ui.js";

export const live = {jobs: []};
const watchers = new Set();
export const watch = fn => { watchers.add(fn); return () => watchers.delete(fn); };
export async function refresh() {
  live.jobs = await api("/api/jobs");
  for (const fn of watchers) fn(live.jobs);
  return live.jobs;
}

// --- theme: system / light / dark, remembered in this browser ---------------------------
export const theme = {
  get: () => store.get("theme", "system"),
  set(value) {
    store.set("theme", value);
    if (value === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", value);
    for (const group of document.querySelectorAll("[data-theme-switch]")) paint(group);
  },
};
function paint(group) {
  for (const button of group.children) button.setAttribute("aria-checked", String(button.dataset.value === theme.get()));
}
export function themeSwitch(labels = false) {
  const group = h("div", {class: "seg", role: "radiogroup", "aria-label": "Motyw", "data-theme-switch": true},
    [["system", "screen", "Systemowy"], ["light", "sun", "Jasny"], ["dark", "moon", "Ciemny"]].map(([value, glyph, text]) =>
      h("button", {type: "button", role: "radio", "data-value": value, title: text, "aria-label": text, onclick: () => theme.set(value)},
        icon(glyph), labels && h("span", {}, text))));
  paint(group);
  return group;
}

// --- top bar: where you are --------------------------------------------------------------
export function setContext(...parts) {          // strings, or [text, href] for the way back
  const nodes = [];
  parts.forEach((part, i) => {
    if (i) nodes.push(h("span", {"aria-hidden": "true"}, "/"));
    nodes.push(Array.isArray(part) ? h("a", {href: part[1]}, part[0]) : h(i === parts.length - 1 ? "b" : "span", {}, part));
  });
  $("#context").replaceChildren(...nodes);
  document.title = `${parts.map(p => Array.isArray(p) ? p[0] : p).reverse().join(" · ")} · insta-roller`;
}

// --- one film, played over the page ------------------------------------------------------
export function play(film) {
  const video = h("video", {controls: true, autoplay: true, playsinline: true, src: film.url, style: "max-height:78vh;width:auto;max-width:100%;display:block;margin:0 auto;background:#000;border-radius:6px"});
  const dialog = h("dialog", {style: "border:1px solid var(--line);border-radius:12px;background:var(--bg);color:var(--ink);padding:16px;max-width:min(94vw,900px);width:max-content"},
    h("div", {class: "spread", style: "margin-bottom:12px;gap:16px"},
      h("div", {style: "min-width:0"}, h("div", {class: "h3"}, film.title), h("div", {class: "mono muted"}, film.name)),
      h("div", {class: "inline"},
        saveButton(`${film.url}?download=1`, film.path),
        h("button", {class: "btn small", type: "button", "aria-label": "Zamknij", onclick: () => dialog.close()}, icon("x")))),
    video);
  dialog.addEventListener("close", () => { video.pause(); dialog.remove(); });
  dialog.addEventListener("click", event => { if (event.target === dialog) dialog.close(); });
  document.body.append(dialog);
  dialog.showModal();
}
