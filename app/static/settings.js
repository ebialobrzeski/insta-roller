// Ustawienia: the few simple ones first; the YAML files of profiles, mounts and themes under "Zaawansowane".
import {$, h, api, icon, guard, toast, size, inWindow, chooseFolder} from "./ui.js";
import {setContext, themeSwitch} from "./live.js";

const QUALITY = [["auto", "Automatycznie"], ["best", "Najlepsza"], ["good", "Dobra"], ["fast", "Szybki podgląd"]];
const FILES = [["profiles", "Zestawy ustawień (lot, urlop)"], ["mounts", "Zamocowania kamery i kadry"], ["themes", "Rodzaje rolek"]];
const ENGINES = [["", "Stałe kadry"], ["local", "AI na tym komputerze"], ["openrouter", "AI przez internet"]];
let advanced = false;

async function loadFile() {
  const name = $("#s-name").value;
  $("#s-yaml").value = name ? (await api(`/api/config/${$("#s-kind").value}/${name}`)).text : "";
}
async function listFiles(keep) {
  const meta = await api("/api/meta"), kind = $("#s-kind").value;
  $("#s-name").replaceChildren(...meta[kind].map(i => h("option", {value: i.name}, i.error ? `${i.name} (błąd w pliku)` : i.name)));
  if (keep) $("#s-name").value = keep;
  await loadFile();
}
async function saveFile() {
  const text = $("#s-yaml").value, kind = $("#s-kind").value;
  const name = (await api(`/api/config/${kind}/validate`, "POST", {text})).name;
  if (name !== $("#s-name").value && !confirm(`Zapisać jako nowy plik „${name}”?`)) return;
  await api(`/api/config/${kind}/${encodeURIComponent(name)}`, "PUT", {text});
  toast(`Zapisano ${name}`);
  await listFiles(name);
}

export async function show(view) {
  setContext("Ustawienia");
  const [s, state] = await Promise.all([api("/api/settings"), api("/api/status")]);
  const put = guard(async (changes, said) => { Object.assign(s, await api("/api/settings", "PUT", changes)); toast(said); });
  const work = h("input", {class: "mono", type: "text", value: s.work, autocomplete: "off", spellcheck: "false", placeholder: "puste: obok rolek, w podfolderze .cache"});
  const quality = h("div", {class: "seg", role: "radiogroup", "aria-label": "Domyślna jakość", style: "flex-wrap:wrap"}, QUALITY.map(([value, text]) =>
    h("button", {type: "button", role: "radio", "aria-checked": String(s.quality === value), onclick: async e => {
      await put({quality: value}, "Zapisano domyślną jakość");
      for (const b of quality.children) b.setAttribute("aria-checked", String(b === e.currentTarget));
    }}, text)));

  // Who frames the shots: chosen once here, used by every new set of reels unless changed there.
  const model = h("input", {type: "text", list: "s-models", value: s.ai_model, autocomplete: "off", spellcheck: "false", placeholder: "anthropic/claude-sonnet-5.5", "aria-label": "Model AI"});
  const models = h("datalist", {id: "s-models"});
  const thinking = h("input", {type: "number", min: 0, max: 200000, step: 1000, inputmode: "numeric", value: s.ai_reasoning || "", placeholder: "0", "aria-label": "Ile AI może się namyślać, w tokenach", style: "max-width:140px"});
  const remote = h("div", {hidden: s.ai_framing !== "openrouter"},
    h("p", {class: state.ai_key ? "muted" : "problem", style: "font-size:13px;margin-top:12px"}, state.ai_key
      ? "Klucz do OpenRouter jest ustawiony. AI dostaje małe klatki z nagrania i opis ujęć; bez danych GPS i nazw plików."
      : h("span", {}, h("b", {}, "Brakuje klucza do OpenRouter."), "Dopisz linię OPENROUTER_API_KEY=… do pliku .env w folderze aplikacji i uruchom ją ponownie. Bez klucza rolki powstaną ze stałymi kadrami.")),
    h("label", {class: "field mt"}, "Model", model, models,
      h("small", {class: "muted"}, "Puste pole: anthropic/claude-sonnet-5.5. Zacznij pisać, żeby wybrać inny z listy OpenRouter.")),
    h("label", {class: "field mt"}, "Ile AI może się namyślać (tokeny)", thinking,
      h("small", {class: "muted"}, "0 = odpowiada od razu. Więcej = lepiej przemyślane rolki, ale drożej i dłużej.")),
    h("button", {class: "btn mt", type: "button", onclick: () => put({ai_model: model.value.trim(), ai_reasoning: +thinking.value || 0}, "Zapisano ustawienia AI")}, "Zapisz"));
  const fill = guard(async () => { if (models.children.length) return; const list = (await api("/api/ai/models")).models || []; models.replaceChildren(...list.map(m => h("option", {value: m.id}, `${m.name}${m.reasoning ? " · z namysłem" : ""} · $${m.prompt_usd_per_m} za mln tokenów`))); });
  if (s.ai_framing === "openrouter") fill();
  const engine = h("div", {class: "seg", role: "radiogroup", "aria-label": "Kto wybiera kadry", style: "flex-wrap:wrap"}, ENGINES.map(([value, text]) =>
    h("button", {type: "button", role: "radio", "aria-checked": String((s.ai_framing === "off" ? "" : s.ai_framing) === value), onclick: async e => {
      await put({ai_framing: value}, "Zapisano");
      for (const b of engine.children) b.setAttribute("aria-checked", String(b === e.currentTarget));
      remote.hidden = value !== "openrouter";
      if (value === "openrouter") fill();
    }}, text)));

  view.replaceChildren(
    h("div", {class: "row"}, h("div", {class: "cell"}, h("h1", {class: "title"}, "Ustawienia"), h("p", {class: "sub"}, "To, co wybierasz raz. Przy każdych nowych rolkach możesz jeszcze zrobić wyjątek."))),
    h("div", {class: "row c2"},
      h("div", {class: "cell"},
        h("h2", {class: "h3"}, "Kto wybiera kadry"),
        h("p", {class: "muted", style: "font-size:14px;margin-top:6px"}, "Stałe kadry: kamera patrzy tam, gdzie przewiduje rodzaj rolki. AI na tym komputerze wybiera najładniejszy z gotowych kadrów. AI przez internet ogląda każde ujęcie dookoła i samo ustawia kamerę: najlepszy efekt, ale płatne."),
        h("div", {class: "mt"}, engine), remote),
      h("div", {class: "cell"},
        h("h2", {class: "h3"}, "Jakość rolek"),
        h("p", {class: "muted", style: "font-size:14px;margin-top:6px"}, "Od niej zaczyna każde nowe zadanie. „Automatycznie” bierze najlepszą dostępną na tym komputerze."),
        h("div", {class: "mt"}, quality),
        h("h2", {class: "h3 mt2"}, "Wygląd aplikacji"),
        h("div", {class: "mt", style: "max-width:360px"}, themeSwitch(true)))),
    h("div", {class: "row c2"},
      h("div", {class: "cell"},
        h("h2", {class: "h3"}, "Miejsce na pliki robocze"),
        h("p", {class: "muted", style: "font-size:14px;margin-top:6px"}, "Podczas pracy powstają duże pliki tymczasowe. Najlepiej trzymać je na szybkim dysku w tym komputerze, nie na dysku sieciowym."),
        h("div", {class: "inline mt"}, work,
          inWindow && h("button", {class: "btn", type: "button", onclick: guard(async () => { const path = await chooseFolder(work.value.trim()); if (path) work.value = path; })}, icon("folder"), "Wybierz…"),
          h("button", {class: "btn", type: "button", onclick: () => put({work: work.value.trim()}, "Zapisano folder plików roboczych")}, "Zapisz")),
        state.cache.free_bytes != null && h("p", {class: "muted", style: "font-size:13px;margin-top:8px"}, `Wolne miejsce: ${size(state.cache.free_bytes)}`)),
      h("div", {class: "cell"},
        h("h2", {class: "h3"}, "Ten komputer"),
        h("dl", {class: "kv", style: "margin-top:10px"},
          h("dt", {}, "Najlepsza jakość"), h("dd", {class: state.stitcher ? "ok" : "muted"}, state.stitcher ? "dostępna" : "niedostępna"),
          h("dt", {}, "Karta graficzna"), h("dd", {class: state.gpu ? "ok" : "muted"}, state.gpu || "nie wykryto"),
          h("dt", {}, "Osoby do rozpoznania"), h("dd", {}, state.people.present ? String(state.people.count) : "brak listy")),
        !state.stitcher && h("p", {class: "muted mt", style: "font-size:13px"}, "Bez programu do sklejania od producenta kamery rolki powstają w jakości „Dobra”: na łączeniu obiektywów bywa widoczny szew."))),
    h("div", {class: "row"}, h("div", {class: "cell"},
      h("details", {open: advanced, ontoggle: guard(async e => { advanced = e.target.open; if (advanced && !$("#s-name").options.length) await listFiles(); })},
        h("summary", {}, "Zaawansowane: pliki ustawień"),
        h("p", {class: "muted", style: "font-size:14px;margin:8px 0 0"}, "Długości rolek, kadry, położenie danych lotu na obrazie i reszta szczegółów jako pliki tekstowe (YAML). Zapis sprawdza poprawność; kolejne rolki używają zmian od razu."),
        h("div", {class: "inline mt"},
          h("select", {id: "s-kind", "aria-label": "Rodzaj pliku", onchange: guard(() => listFiles())}, FILES.map(([value, text]) => h("option", {value}, text))),
          h("select", {id: "s-name", "aria-label": "Plik", onchange: guard(loadFile)})),
        h("textarea", {id: "s-yaml", rows: 22, spellcheck: "false", "aria-label": "Treść pliku", style: "margin-top:12px"}),
        h("div", {class: "inline mt"},
          h("button", {class: "btn", type: "button", onclick: guard(async () => { await api(`/api/config/${$("#s-kind").value}/validate`, "POST", {text: $("#s-yaml").value}); toast("Plik jest poprawny"); })}, "Sprawdź"),
          h("button", {class: "btn primary", type: "button", onclick: guard(saveFile)}, "Zapisz"),
          h("span", {class: "muted", style: "font-size:13px"}, "Nowy plik: zmień pole name i zapisz.")),
        h("dl", {class: "kv mt2"}, h("dt", {}, "Pliki ustawień"), h("dd", {class: "mono"}, s.profiles), h("dt", {}, "Dane aplikacji"), h("dd", {class: "mono"}, s.data))))));
  if (advanced) await guard(() => listFiles())();
}
