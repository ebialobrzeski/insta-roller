// Ustawienia: the few simple ones first; the YAML files of profiles, mounts and themes under "Zaawansowane".
import {$, h, api, guard, toast, size} from "./ui.js";
import {setContext, themeSwitch} from "./live.js";

const QUALITY = [["auto", "Automatycznie"], ["best", "Najlepsza"], ["good", "Dobra"], ["fast", "Szybki podgląd"]];
const FILES = [["profiles", "Profile (rodzaje filmów)"], ["mounts", "Zamocowania kamery"], ["themes", "Tematy filmów"]];
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
  const work = h("input", {class: "mono", type: "text", value: s.work, autocomplete: "off", spellcheck: "false", placeholder: "puste: obok filmów, w podfolderze .cache"});
  const quality = h("div", {class: "seg", role: "radiogroup", "aria-label": "Domyślna jakość", style: "flex-wrap:wrap"}, QUALITY.map(([value, text]) =>
    h("button", {type: "button", role: "radio", "aria-checked": String(s.quality === value), onclick: async e => {
      await put({quality: value}, "Zapisano domyślną jakość");
      for (const b of quality.children) b.setAttribute("aria-checked", String(b === e.currentTarget));
    }}, text)));

  view.replaceChildren(
    h("div", {class: "row"}, h("div", {class: "cell"}, h("h1", {class: "title"}, "Ustawienia"), h("p", {class: "sub"}, "Zwykle nie trzeba tu nic zmieniać."))),
    h("div", {class: "row c2"},
      h("div", {class: "cell"},
        h("h2", {class: "h3"}, "Miejsce na pliki robocze"),
        h("p", {class: "muted", style: "font-size:14px;margin-top:6px"}, "Podczas pracy powstają duże pliki tymczasowe. Najlepiej trzymać je na szybkim dysku w tym komputerze, nie na dysku sieciowym."),
        h("div", {class: "inline mt"}, work, h("button", {class: "btn", type: "button", onclick: () => put({work: work.value.trim()}, "Zapisano folder plików roboczych")}, "Zapisz")),
        state.cache.free_bytes != null && h("p", {class: "muted", style: "font-size:13px;margin-top:8px"}, `Wolne miejsce: ${size(state.cache.free_bytes)}`)),
      h("div", {class: "cell"},
        h("h2", {class: "h3"}, "Domyślna jakość"),
        h("p", {class: "muted", style: "font-size:14px;margin-top:6px"}, "Od niej zaczyna kreator nowego filmu. Przy każdym filmie można wybrać inną."),
        h("div", {class: "mt"}, quality),
        h("h2", {class: "h3 mt2"}, "Wygląd"),
        h("div", {class: "mt", style: "max-width:360px"}, themeSwitch(true)))),
    h("div", {class: "row"}, h("div", {class: "cell"},
      h("details", {open: advanced, ontoggle: guard(async e => { advanced = e.target.open; if (advanced && !$("#s-name").options.length) await listFiles(); })},
        h("summary", {}, "Zaawansowane: pliki ustawień"),
        h("p", {class: "muted", style: "font-size:14px;margin:8px 0 0"}, "Profile, zamocowania kamery i tematy filmów jako pliki YAML. Zapis sprawdza poprawność; kolejne filmy używają zmian od razu."),
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
