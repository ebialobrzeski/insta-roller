// insta-roller UI: plain ES modules, no build step. This file is the shell: sidebar, routing, the status row.
import {$, h, icon, mark, store, toast, stateText, inWindow} from "./ui.js";
import {live, watch, refresh, themeSwitch} from "./live.js";
import * as home from "./home.js";
import * as wizard from "./wizard.js";
import * as films from "./films.js";
import * as people from "./people.js";
import * as settings from "./settings.js";

const SCREENS = {przeglad: home, nowy: wizard, filmy: films, osoby: people, ustawienia: settings};
const NAV = [["przeglad", "home", "Przegląd"], ["nowy", "plus", "Nowe rolki"], null,
             ["filmy", "film", "Moje nagrania"], ["osoby", "people", "Osoby"], null, ["ustawienia", "gear", "Ustawienia"]];

// --- the shell --------------------------------------------------------------------------
$("#brand").prepend(mark());
$("#nav").replaceChildren(...NAV.map(item => item === null ? h("hr") :
  h("a", {href: `#${item[0]}`, "data-nav": item[0]}, icon(item[1]), h("span", {}, item[2]),
    item[0] === "filmy" && h("span", {class: "count", id: "nav-count", hidden: true}))));
$("#theme").replaceWith(themeSwitch());
$("#collapse").append(icon("left"), h("span", {}, "Zwiń"));
$("#menu").append(icon("menu"));
$("#top-films").remove();                       // the menu already has it
$("#top-new").append(icon("plus"), h("span", {}, "Nowe rolki"));

const app = $("#app");
app.classList.toggle("narrow", store.get("narrow", false));
$("#collapse").addEventListener("click", () => store.set("narrow", app.classList.toggle("narrow")));
$("#collapse").setAttribute("aria-label", "Zwiń albo rozwiń menu");
$("#menu").addEventListener("click", () => app.classList.toggle("open"));
$("#scrim").addEventListener("click", () => app.classList.remove("open"));
$("#side").addEventListener("click", event => { if (event.target.closest("a")) app.classList.remove("open"); });

// The status row and the notice card: what the app is doing, and what waits for the user.
watch(jobs => {
  const running = jobs.find(j => j.status === "running"), waiting = jobs.filter(j => j.status === "queued").length;
  const now = $("#now");
  now.classList.toggle("busy", !!running);
  now.href = running ? `#filmy/${running.id}` : "#filmy";
  $("#now-text").textContent = running ? stateText(running) : waiting ? "Zaraz zaczynam…" : "Bezczynna";
  now.title = running ? `${running.label}: ${stateText(running)}` : "Aplikacja nic teraz nie robi";
  const active = jobs.filter(j => j.status === "running" || j.status === "queued").length;
  $("#nav-count").hidden = !active;
  $("#nav-count").textContent = active;

  const review = jobs.find(j => j.status === "review"), failed = jobs.find(j => j.status === "error");
  const [href, glyph, head, text] =
    review ? [`#filmy/${review.id}/ujecia`, "eye", "Czeka na Ciebie", `Sprawdź ujęcia i zrób rolki: ${review.label}`]
    : failed ? [`#filmy/${failed.id}`, "alert", "Coś się nie udało", `Zobacz, co poszło nie tak: ${failed.label}`]
    : running ? [`#filmy/${running.id}`, "info", "Pracuję w tle", inWindow ? "Możesz schować okno do ikony przy zegarze. Dam znać tutaj, gdy będzie na co popatrzeć." : "Możesz zamknąć kartę. Rolki znajdziesz w „Moje nagrania”."]
    : jobs.length ? ["#nowy", "plus", "Nowe rolki", "Wskaż kolejne nagrania albo zrób inne rolki z tych, które już są."]
    : ["#nowy", "film", "Zacznij tutaj", "Wskaż folder z nagraniami, a aplikacja wybierze najlepsze momenty."];
  const notice = $("#notice"), key = href + head + text;
  if (notice.dataset.key !== key) {              // only when it changes: no flicker while polling
    notice.dataset.key = key;
    notice.href = href;
    notice.replaceChildren(icon(glyph), h("div", {}, h("b", {}, head), text));
  }
});

// --- routing ------------------------------------------------------------------------------
let leave = null, turn = 0;
async function route() {
  const [name, ...args] = (location.hash.slice(1) || "przeglad").split("/");
  const screen = SCREENS[name] ? name : "przeglad", mine = ++turn;
  for (const link of document.querySelectorAll("[data-nav]")) {
    if (link.dataset.nav === screen) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  }
  $("#top-new").hidden = screen === "nowy";
  if (leave) { try { leave(); } catch (error) { /* a screen's clean-up must not block the next one */ } leave = null; }
  const view = $("#view");
  try {
    const cleanup = await SCREENS[screen].show(view, args);
    if (mine === turn) leave = cleanup || null; else if (cleanup) cleanup();
  } catch (error) {
    toast(error.message, true);
  }
  if (mine === turn && !args.length) window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);

async function poll() {
  if (!document.hidden) { try { await refresh(); } catch (error) { $("#now-text").textContent = "Aplikacja nie odpowiada"; } }
  setTimeout(poll, live.jobs.some(j => j.status === "running" || j.status === "queued") ? 1500 : 4000);
}
await refresh().catch(() => {});
route();
setTimeout(poll, 1500);
