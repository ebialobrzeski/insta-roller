// Nowy film: three steps. 1 Nagrania, 2 Co ma powstać, 3 Jakość i start.
import {$, h, api, icon, guard, toast, store, duration, plural, THEMES, inWindow, chooseFolder} from "./ui.js";
import {setContext} from "./live.js";

const CUSTOM = "__custom__";
const STEPS = ["Nagrania", "Co ma powstać", "Jakość i start"];
// Seconds of work for one second of film, by quality: honest orders of magnitude, not promises.
const QUALITIES = [
  ["best", "Najlepsza", "Pełna jakość z plików kamery, sklejona programem producenta. Bez szwu.", 15],
  ["good", "Dobra", "Pełna jakość z plików kamery, sklejona przez FFmpeg. Na łączeniu obiektywów bywa widoczny szew.", 5],
  ["fast", "Szybki podgląd", "Z małych plików podglądu kamery. Żeby sprawdzić, co wyjdzie, zanim zrobisz film na czysto.", 1],
];
let view, meta, w;

function fresh(preset) {
  return {step: 1, preset, listing: null, found: null, checking: false, kind: preset === "lot" ? "flight" : preset === "urlop" ? "holiday" : null,
          themes: null, overlay: null, ai: null, aiModel: "", aiReasoning: "", lengths: {}, mode: "day", length: "", people: null, review: false, shots: preset === "ujecia", films: preset !== "ujecia",
          quality: null, out: "", profile: preset === "wlasne" ? CUSTOM : "auto", mount: "", yaml: "", more: preset === "wlasne"};
}

// --- what the choices mean ---------------------------------------------------------------
const kind = () => w.kind || (w.found && w.found.kind) || "flight";
function profileName() {                        // the profile the CLI will load
  if (w.profile !== "auto" && w.profile !== CUSTOM) return w.profile;
  return w.kind ? (w.kind === "flight" ? "flight" : "holiday") : "auto";
}
function profile() {
  const name = w.profile === CUSTOM ? ($("#c-base") && $("#c-base").value) || "flight" : profileName();
  const wanted = name === "auto" ? (kind() === "flight" ? "flight" : "holiday") : name;
  return meta.profiles.find(p => p.name === wanted && !p.error) || meta.profiles.find(p => !p.error);
}
const flight = () => (profile() || {}).kind !== "holiday";
function themes() {                             // [{name, title, text, length, on}]
  const p = profile();
  return (p ? p.themes : []).map(name => {
    const t = meta.themes.find(x => x.name === name) || {name, enabled: true};
    const [title, text] = THEMES[name] || [t.title || name, ""];
    return {name, title: t.title || title, text, length: +w.lengths[name] || +w.length || t.length || p.length, usual: t.length || p.length, def: !!t.enabled,
            on: w.themes && name in w.themes ? w.themes[name] : !!t.enabled};
  });
}
function filmSeconds() {
  const p = profile() || {};
  if (flight()) return themes().filter(t => t.on).reduce((sum, t) => sum + t.length, 0);
  return (+w.length || p.length || 60) * (w.mode === "both" ? 2 : 1);
}
function quality() {
  const f = w.found || {}, full = f.full_quality > 0;
  const can = {best: meta.stitcher && full, good: full || f.films > 0 || !f.previews, fast: f.previews > 0};
  const wish = w.quality || (meta.quality !== "auto" ? meta.quality : null);
  return {can, full, value: wish && can[wish] ? wish : can.best ? "best" : can.good ? "good" : "fast"};
}
const about = s => s < 90 ? "około minuty" : s < 3600 ? `około ${Math.round(s / 60)} min` : `około ${(s / 3600).toFixed(1).replace(".", ",")} godz.`;

// --- step 1: the folder ---------------------------------------------------------------------
const browse = guard(async path => {
  try { w.listing = await api(`/api/folders?path=${encodeURIComponent(path)}`); }
  catch (error) { if (!w.listing) w.listing = await api("/api/folders?path="); throw error; }
  finally { draw(); }
  w.found = null;
  const here = w.listing;
  if (here.path) store.set("folder", here.path);
  if (here.path && here.videos) {               // reading the files takes a moment, more on a network drive
    w.checking = true; draw();
    try { const found = await api(`/api/folders/inspect?path=${encodeURIComponent(here.path)}`); if (w.listing === here) w.found = found; }
    finally { if (w.listing === here) { w.checking = false; draw(); } }
  } else draw();
});

function found() {
  const l = w.listing, f = w.found;
  if (!l.path) return h("div", {class: "found"}, icon("folder"), h("div", {}, h("div", {class: "h3"}, "Wybierz folder z nagraniami"),
    h("p", {class: "muted"}, "Zacznij od dysku albo wklej ścieżkę. Dysk sieciowy (NAS) też się nada.")));
  if (w.checking) return h("div", {class: "found"}, icon("refresh"), h("div", {}, h("div", {class: "h3"}, "Sprawdzam nagrania…"),
    h("p", {class: "muted"}, "Czytam długość plików. Na dysku sieciowym to chwilę trwa.")));
  if (!f || !f.usable) return h("div", {class: "found warn"}, icon("alert"), h("div", {}, h("div", {class: "h3"}, "Nie widzę tu nagrań z kamery"),
    h("p", {class: "muted"}, l.dirs.length ? "W tym folderze nie ma plików z kamery. Wejdź do podfolderu z nagraniami."
      : "Szukam plików .insv albo .lrv z kamery Insta360 (ewentualnie gotowych .mp4 z Insta360 Studio). Wybierz inny folder.")));
  const yes = (ok, a, b) => h("li", {}, ok ? h("b", {}, a) : b);
  return h("div", {}, h("div", {class: "found"}, icon("check"), h("div", {},
    h("div", {class: "h3"}, f.recordings == null ? "Są tu nagrania" : `${plural(f.recordings, "nagranie", "nagrania", "nagrań")}${f.duration_s ? `, razem ${duration(f.duration_s)}` : ""}`),
    h("ul", {class: "facts"},
      yes(f.full_quality, "Są pliki w pełnej jakości.", "Nie ma plików w pełnej jakości (.insv)."),
      yes(f.previews, "Są pliki podglądu z kamery.", "Nie ma plików podglądu."),
      yes(f.igc, "Jest zapis lotu (IGC).", "Nie ma zapisu lotu (IGC)."),
      l.job && h("li", {}, `Z tego folderu powstał już film (nr ${l.job}). Przerobię tylko to, co nowe.`)))),
    h("div", {class: "mt2"}, h("div", {class: "muted", style: "font-size:13px;margin-bottom:8px"},
        w.kind && w.kind !== f.kind ? "Twój wybór:" : f.kind === "flight" ? "Wygląda na lot. Jeśli nie, zmień:" : "Wygląda na urlop. Jeśli to lot bez zapisu IGC, zmień:"),
      h("div", {class: "seg", style: "max-width:260px"}, [["flight", "Lot"], ["holiday", "Urlop"]].map(([value, text]) =>
        h("button", {type: "button", "aria-pressed": String(kind() === value), onclick: () => { w.kind = value; w.themes = null; draw(); }}, text)))));
}

function stepFolder() {
  const l = w.listing;
  const go = () => browse($("#f-path").value);
  const recent = !l.path && l.recent && l.recent.length > 0;
  return [
    h("div", {class: "row"}, h("div", {class: "cell"}, h("h1", {class: "title"}, "Gdzie są nagrania?"),
      h("p", {class: "sub"}, "Wskaż folder z plikami z kamery. Niczego w nim nie zmienię: filmy trafią do osobnego podfolderu."))),
    h("div", {class: "row wide"},
      h("div", {class: "cell flush"},
        h("div", {style: "padding:20px 32px 12px"},
          inWindow && h("div", {class: "inline", style: "margin-bottom:14px"},
            h("button", {class: "btn primary", type: "button", onclick: guard(async () => { const path = await chooseFolder(l.path); if (path) browse(path); })},
              icon("folder"), "Wybierz folder…"),
            h("span", {class: "muted", style: "font-size:13px"}, "albo wklej ścieżkę lub wskaż folder poniżej")),
          h("div", {class: "inline"},
            h("input", {id: "f-path", class: "mono", type: "text", value: l.path, autocomplete: "off", spellcheck: "false", "aria-label": "Ścieżka folderu",
                        placeholder: "Wklej ścieżkę, np. D:\\Wideo\\lot albo \\\\nas\\wideo\\lot", onkeydown: e => { if (e.key === "Enter") go(); }}),
            h("button", {class: "btn", type: "button", onclick: go}, "Przejdź")),
          h("div", {class: "crumbs", style: "margin-top:12px"},
            h("button", {type: "button", onclick: () => browse("")}, "Ten komputer"),
            l.crumbs.map(c => [h("i", {}, "›"), h("button", {type: "button", onclick: () => browse(c.path)}, c.name)]))),
        recent && h("div", {class: "muted", style: "padding:8px 32px;font-size:13px"}, "Ostatnio używane"),
        recent && h("ul", {class: "dirs"}, l.recent.map(r => h("li", {}, h("button", {type: "button", onclick: () => browse(r.path)}, icon("film"),
          h("span", {}, r.name), h("span", {class: "muted", style: "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px"}, r.path))))),
        recent && h("div", {class: "muted", style: "padding:16px 32px 8px;font-size:13px;border-top:1px solid var(--line)"}, "Dyski tego komputera"),
        h("ul", {class: "dirs"}, l.dirs.map(d => h("li", {}, h("button", {type: "button", onclick: () => browse(d.path)}, icon("folder"), d.name))),
          !l.dirs.length && h("li", {}, h("div", {class: "muted", style: "padding:12px 32px;font-size:14px"}, "Ten folder nie ma podfolderów.")))),
      h("div", {class: "stack"},
        h("div", {class: "cell"}, found()))),
    bar(null, h("button", {class: "btn primary", type: "button", disabled: !(w.found && w.found.usable), onclick: () => { w.step = 2; draw(); }}, "Dalej", icon("right"))),
  ];
}

// --- step 2: what to make ---------------------------------------------------------------------
const pick = (on, title, text, tags, onclick, disabled) => h("button", {class: "cell pick", type: "button", "aria-pressed": String(!!on), onclick, disabled},
  h("span", {class: "tick"}, icon("check")),
  h("div", {class: "h3", style: "padding-right:28px"}, title), h("p", {class: "muted", style: "margin-top:6px;font-size:14px"}, text),
  tags && h("div", {class: "tags", style: "margin-top:12px"}, tags));
let aiModels;                                   // undefined = not asked, null = loading, [] = no list
async function loadModels() {
  if (aiModels !== undefined || (w.ai || (profile() || {}).ai) !== "openrouter") return;
  aiModels = null;
  try { aiModels = (await api("/api/ai/models")).models || []; } catch { aiModels = []; }
  if (w && w.step === 2) draw();
}
function reasoningNote() {
  const chosen = (aiModels || []).find(m => m.id === (w.aiModel || (profile() || {}).ai_model));
  if (chosen && !chosen.reasoning) return "Ten model nie ma trybu reasoning: liczba zostanie pominięta.";
  return "Ile tokenów model może zużyć na zastanowienie się przed odpowiedzią. 0 = bez reasoningu. Więcej = lepiej przemyślany film, ale drożej i dłużej.";
}
const toggle = (key, title, text) => h("label", {class: "switch"},
  h("input", {type: "checkbox", checked: !!w[key], onchange: e => { w[key] = e.target.checked; draw(); }}), h("span", {}, title, h("small", {}, text)));

function stepWhat() {
  const p = profile() || {}, rows = [];
  rows.push(h("div", {class: "row"}, h("div", {class: "cell"}, h("h1", {class: "title"}, "Co ma powstać?"),
    h("p", {class: "sub"}, !w.films ? "Same ujęcia: najlepsze momenty jako osobne pliki, bez gotowego filmu."
      : flight() ? "Z jednego lotu powstaje kilka krótkich filmów. Zaznacz te, które chcesz." : "Wybierz, jak podzielić nagrania z wyjazdu."))));
  if (flight()) {
    rows.push(h("div", {class: "row c3"}, themes().map(t => pick(t.on, t.title, t.text, [h("span", {class: "tag"}, `${Math.round(t.length)} s`), !t.def && h("span", {class: "tag plain"}, "domyślnie wyłączone")],
      () => { w.themes = {...Object.fromEntries(themes().map(x => [x.name, x.on])), [t.name]: !t.on}; draw(); }))));
  } else {
    rows.push(h("div", {class: "row c3"}, [["day", "Film na każdy dzień", "Osobny film z każdego dnia wyjazdu."], ["trip", "Jeden film z wyjazdu", "Wszystkie dni w jednym filmie."],
      ["both", "Jedno i drugie", "Film z każdego dnia i dodatkowo jeden z całości."]].map(([value, title, text]) => pick(w.mode === value, title, text, null, () => { w.mode = value; draw(); }))));
  }
  const overlayOn = w.overlay == null ? !!p.overlay : w.overlay;
  const mine = w.people || p.people || [];
  rows.push(h("div", {class: "row c2"},
    flight() ? h("div", {class: "cell"}, h("div", {class: "h3"}, "Długość filmów"),
        h("p", {class: "muted", style: "font-size:13px;margin:4px 0 10px"}, "W sekundach. Gdy materiału jest mniej, film wyjdzie krótszy."),
        h("div", {class: "lengths"}, themes().filter(t => t.on).map(t => h("label", {class: "length"}, h("span", {}, t.title),
          h("input", {type: "number", min: 5, max: 600, step: 5, inputmode: "numeric", "aria-label": `Długość filmu ${t.title} w sekundach`,
            value: w.lengths[t.name] || "", placeholder: String(Math.round(+w.length || t.usual)),
            oninput: e => { w.lengths[t.name] = e.target.value; }, onchange: () => draw()}), h("span", {class: "muted"}, "s")))),
        h("div", {class: "h3", style: "margin-top:20px"}, "Na obrazie"),
        h("label", {class: "switch"}, h("input", {type: "checkbox", checked: overlayOn, onchange: e => { w.overlay = e.target.checked; }}),
          h("span", {}, "Dane lotu na obrazie", h("small", {}, "Prędkość, wysokość i wznoszenie w rogu kadru."))))
      : h("div", {class: "cell"}, h("div", {class: "h3"}, "Długość i osoby"),
        h("label", {class: "field mt"}, "Długość jednego filmu (sekundy)",
          h("input", {type: "number", min: 5, step: 5, inputmode: "numeric", value: w.length, placeholder: `${p.length || 60} (zwykle)`, oninput: e => { w.length = e.target.value; }})),
        h("div", {class: "field mt"}, "Kogo szukać w nagraniach"),
        meta.people.length ? h("div", {class: "checks", style: "margin-top:8px"}, meta.people.map(name => h("label", {class: "check"},
            h("input", {type: "checkbox", checked: mine.includes(name), onchange: e => { const set = new Set(w.people || p.people || []); e.target.checked ? set.add(name) : set.delete(name); w.people = [...set]; }}), name)))
          : h("p", {class: "muted", style: "font-size:14px;margin-top:6px"}, "Lista osób jest pusta. Dodasz je w zakładce „Osoby”.")),
    h("div", {class: "cell"}, h("div", {class: "h3"}, "Zanim powstanie film"),
      h("label", {class: "field"}, "AI Framing: kto wybiera kadry",
        h("select", {onchange: e => { w.ai = e.target.value; loadModels(); draw(); }},
          [["off", "Stałe kadry z tematu"], ["local", "AI na tym komputerze (model lokalny)"], ["openrouter", "AI przez OpenRouter (miniatury klatek wychodzą do internetu)"]]
            .map(([value, text]) => h("option", {value, selected: value === (w.ai || p.ai || "off")}, text))),
        h("small", {class: "muted"}, "AI ogląda możliwe kadry każdego ujęcia i wybiera najlepszy. OpenRouter wymaga klucza OPENROUTER_API_KEY w pliku .env.")),
      (w.ai || p.ai) === "openrouter" && h("div", {class: "ai-more"},
        h("label", {class: "field"}, "Model",
          h("input", {type: "text", list: "ai-models", value: w.aiModel, placeholder: p.ai_model || "", autocomplete: "off", spellcheck: "false",
            oninput: e => { w.aiModel = e.target.value.trim(); }, onchange: () => draw()}),
          h("datalist", {id: "ai-models"}, (aiModels || []).map(m => h("option", {value: m.id}, `${m.name}${m.reasoning ? " · reasoning" : ""} · $${m.prompt_usd_per_m}/mln tokenów`))),
          h("small", {class: "muted"}, aiModels === null ? "Wczytuję listę modeli z OpenRouter…"
            : aiModels.length ? `Zacznij pisać, żeby wybrać z ${aiModels.length} modeli, które przyjmują obrazy. Puste pole: ${p.ai_model}.`
            : "Nie udało się pobrać listy modeli. Wpisz nazwę ręcznie, np. anthropic/claude-sonnet-5.5.")),
        h("label", {class: "field"}, "Tokeny na reasoning",
          h("input", {type: "number", min: 0, max: 200000, step: 1000, inputmode: "numeric", value: w.aiReasoning, placeholder: String(p.ai_reasoning || 0),
            oninput: e => { w.aiReasoning = e.target.value; }}),
          h("small", {class: "muted"}, reasoningNote()))),
      toggle("review", "Chcę najpierw przejrzeć ujęcia", "Aplikacja zatrzyma się po wybraniu ujęć. Poprawisz je i dopiero wtedy ruszy render."),
      w.films ? toggle("shots", "Zapisz też osobne ujęcia", "Każde ujęcie dodatkowo jako oddzielny plik, do montażu w telefonie.")
        : toggle("films", "Zrób też gotowy film", "Oprócz osobnych ujęć sklej je w film."))));
  const nothing = flight() && !themes().some(t => t.on);
  rows.push(bar(h("button", {class: "btn", type: "button", onclick: () => { w.step = 1; draw(); }}, "Wstecz"),
    h("button", {class: "btn primary", type: "button", disabled: nothing, onclick: () => { w.step = 3; draw(); }}, "Dalej", icon("right")),
    nothing && "Zaznacz przynajmniej jeden film."));
  return rows;
}

// --- step 3: quality and start ------------------------------------------------------------------
function summary() {
  const q = quality(), factor = QUALITIES.find(x => x[0] === q.value)[3], film = filmSeconds();
  let what;
  if (flight()) {
    const on = themes().filter(t => t.on);
    const many = on.length > 1 && on.length < 5;
    what = `${many ? "Powstaną" : "Powstanie"} ${plural(on.length, "film", "filmy", "filmów")} z lotu (${on.map(t => t.title).join(", ")}), razem około ${Math.round(film)} s`;
    if (!w.films) what = `Powstaną osobne ujęcia z lotu (${on.map(t => t.title).join(", ")}), bez gotowego filmu`;
  } else {
    what = "Powstanie " + {day: "film z każdego dnia wyjazdu", trip: "jeden film z całego wyjazdu", both: "film z każdego dnia i jeden z całego wyjazdu"}[w.mode];
    if (!w.films) what = "Powstaną osobne ujęcia z wyjazdu, bez gotowego filmu";
  }
  const lead = what;
  return `${lead}. ${w.review ? "Najpierw pokażę wybrane ujęcia do przejrzenia. Potem render potrwa" : "Potrwa to"} ${about(film * factor + 60)}${flight() ? "" : " na każdy film"}.`;
}

function more() {
  const names = meta.profiles.filter(p => !p.error).map(p => p.name), custom = w.profile === CUSTOM;
  const opt = (value, text, chosen) => h("option", {value, selected: value === chosen}, text);
  return h("details", {open: w.more, ontoggle: e => { w.more = e.target.open; }},
    h("summary", {}, "Więcej ustawień"),
    h("div", {class: "row c3", style: "margin-top:12px"},
      h("div", {class: "cell tight"}, h("label", {class: "field"}, "Zestaw ustawień (profil)",
        h("select", {onchange: e => { w.profile = e.target.value; w.themes = null; draw(); }},
          opt("auto", "Automatycznie", w.profile), names.map(n => opt(n, n, w.profile)), opt(CUSTOM, "Własne, tylko dla tego filmu", w.profile)))),
      h("div", {class: "cell tight"}, h("label", {class: "field"}, "Zamocowanie kamery",
        h("select", {onchange: e => { w.mount = e.target.value; }}, opt("", "Jak w profilu", w.mount), meta.mounts.map(m => opt(m.name, m.name, w.mount))))),
      h("div", {class: "cell tight"}, h("label", {class: "field"}, flight() ? "Długość każdego filmu (sekundy)" : "Długość filmu (sekundy)",
        h("input", {type: "number", min: 5, step: 1, value: w.length, placeholder: "jak w profilu", oninput: e => { w.length = e.target.value; }})))),
    custom && h("div", {class: "row"}, h("div", {class: "cell"},
      h("div", {class: "h3"}, "Własne ustawienia tego filmu"),
      h("div", {class: "inline mt"},
        h("select", {id: "c-base", "aria-label": "Na podstawie profilu", onchange: guard(loadBase)}, names.map(n => opt(n, `Na podstawie: ${n}`))),
        h("select", {id: "c-format", "aria-label": "Format"}, opt("", "Format bez zmiany"), opt("1080x1920", "Pionowy 9:16"), opt("1920x1080", "Poziomy 16:9"), opt("1080x1080", "Kwadrat 1:1")),
        h("select", {id: "c-audio", "aria-label": "Dźwięk"}, opt("", "Dźwięk bez zmiany"), opt("1", "Dźwięk z nagrania"), opt("0", "Bez dźwięku")),
        h("button", {class: "btn", type: "button", onclick: guard(compose)}, "Wstaw do ustawień")),
      h("label", {class: "field mt"}, "Wszystkie ustawienia (YAML)",
        h("textarea", {id: "c-yaml", rows: 14, spellcheck: "false", value: w.yaml, oninput: e => { w.yaml = e.target.value; }})),
      h("div", {class: "inline mt"},
        h("button", {class: "btn", type: "button", onclick: guard(async () => { await api("/api/config/profiles/validate", "POST", {text: w.yaml}); toast("Ustawienia są poprawne"); })}, "Sprawdź"),
        h("input", {id: "c-name", type: "text", placeholder: "nazwa_nowego_profilu", autocomplete: "off", "aria-label": "Nazwa nowego profilu"}),
        h("button", {class: "btn", type: "button", onclick: guard(saveCustom)}, "Zapisz jako nowy profil")))));
}

async function loadBase() { w.yaml = (await api(`/api/config/profiles/${$("#c-base").value}`)).text; $("#c-yaml").value = w.yaml; }
async function compose() {
  const changes = {};
  if ($("#c-format").value) { const [width, height] = $("#c-format").value.split("x").map(Number); changes.output = {width, height}; }
  if ($("#c-audio").value) changes.audio = $("#c-audio").value === "1";
  if (+w.length) changes.target_length_s = +w.length;
  if (w.mount) changes.mount = w.mount;
  w.yaml = (await api("/api/config/compose", "POST", {base: $("#c-base").value, changes})).text;
  $("#c-yaml").value = w.yaml;
}
async function saveCustom() {
  const name = $("#c-name").value.trim();
  if (!name) throw new Error("Podaj nazwę nowego profilu");
  await api(`/api/config/profiles/${encodeURIComponent(name)}`, "PUT", {text: w.yaml.replace(/^name:.*$/m, `name: ${name}`)});
  meta = await api("/api/meta");
  w.profile = name;
  toast(`Zapisano profil ${name}`);
  draw();
}

function stepStart() {
  const q = quality(), film = filmSeconds();
  const why = {best: q.full ? "Niedostępna na tym komputerze: brak programu do sklejania Media SDK." : "Niedostępna: w folderze nie ma plików w pełnej jakości (.insv), są tylko podglądy.",
               good: "Niedostępna: w folderze nie ma plików w pełnej jakości (.insv), są tylko podglądy.",
               fast: "Niedostępny: w folderze nie ma plików podglądu z kamery."};
  return [
    h("div", {class: "row"}, h("div", {class: "cell"}, h("h1", {class: "title"}, "Jakość i start"),
      h("p", {class: "sub"}, "Im lepsza jakość, tym dłużej to trwa. Komputer może w tym czasie robić coś innego."))),
    h("div", {class: "row c3"}, QUALITIES.map(([value, title, text, factor]) => pick(q.value === value, title, q.can[value] ? text : why[value],
      [h("span", {class: "tag"}, value === "fast" ? "poniżej minuty na film" : `ok. ${factor} s pracy na sekundę filmu`), q.can[value] && value !== "fast" && h("span", {class: "tag plain"}, about(film * factor + 60))],
      () => { w.quality = value; draw(); }, !q.can[value]))),
    h("div", {class: "row"}, h("div", {class: "cell"},
      h("label", {class: "field"}, "Gdzie zapisać filmy",
        h("input", {class: "mono", type: "text", value: w.out, autocomplete: "off", spellcheck: "false", placeholder: w.found ? w.found.out : "", oninput: e => { w.out = e.target.value; }})),
      inWindow && h("button", {class: "btn small", type: "button", style: "margin-top:8px", onclick: guard(async () => {
        const path = await chooseFolder(w.out || w.listing.path);
        if (path) { w.out = path; draw(); }
      })}, icon("folder"), "Wybierz inny folder…"),
      h("p", {class: "muted", style: "font-size:13px;margin-top:8px"}, "Puste pole oznacza folder pokazany wyżej: podfolder obok nagrań. Możesz wpisać inny, na przykład na szybszym dysku."))),
    h("div", {class: "row"}, h("div", {class: "cell"}, h("div", {class: "spread"},
      h("div", {style: "flex:1 1 320px;min-width:0"}, h("div", {class: "h3"}, w.listing.path.split(/[\\/]/).filter(Boolean).pop() || w.listing.path), h("p", {class: "sub", id: "w-summary"}, summary())),
      h("button", {class: "btn primary big", type: "button", onclick: guard(submit)}, icon("film"), w.films ? "Zrób film" : "Zrób ujęcia")))),
    h("div", {class: "row"}, h("div", {class: "cell"}, more())),
    bar(h("button", {class: "btn", type: "button", onclick: () => { w.step = 2; draw(); }}, "Wstecz")),
  ];
}

async function submit() {
  const p = profile() || {}, custom = w.profile === CUSTOM, list = themes();
  const people = w.people && !flight() && JSON.stringify([...w.people].sort()) !== JSON.stringify([...(p.people || [])].sort()) ? w.people : [];
  const job = await api("/api/jobs", "POST", {folder: w.listing.path, options: {
    out: w.out.trim(), profile: custom ? "auto" : profileName(), profile_yaml: custom ? w.yaml : "", mount: w.mount,
    length: +w.length || null,
    lengths: flight() ? Object.fromEntries(list.filter(t => t.on && +w.lengths[t.name] >= 5 && +w.lengths[t.name] !== t.usual).map(t => [t.name, +w.lengths[t.name]])) : {},
    enable: flight() ? list.filter(t => t.on && !t.def).map(t => t.name) : [],
    disable: flight() ? list.filter(t => !t.on && t.def).map(t => t.name) : [],
    mode: flight() ? "day" : w.mode, people,
    overlay: w.overlay == null || w.overlay === !!p.overlay ? null : w.overlay,
    ai_framing: w.ai && w.ai !== (p.ai || "off") ? w.ai : null,
    ai_model: (w.ai || p.ai) === "openrouter" && w.aiModel && w.aiModel !== p.ai_model ? w.aiModel : "",
    ai_reasoning: (w.ai || p.ai) === "openrouter" && w.aiReasoning !== "" && +w.aiReasoning !== (p.ai_reasoning || 0) ? +w.aiReasoning : null,
    review: w.review, shots: w.shots, films: w.films, quality: quality().value,
  }});
  toast(job.existing && job.status === "running" ? "Ten film właśnie powstaje, nic nie zmieniłem" : "Zaczynam. Postęp zobaczysz tutaj.");
  w = null;
  location.hash = `#filmy/${job.id}`;
}

// --- the frame of every step ------------------------------------------------------------------
const bar = (back, next, note) => h("div", {class: "bar"}, back, h("span", {class: "grow muted", style: "font-size:13px;text-align:right"}, note || ""), next);

function draw() {
  if (!w || !view.isConnected) return;
  setContext("Nowy film", STEPS[w.step - 1]);
  const stepper = h("div", {class: "row"}, h("ol", {class: "stepper", "aria-label": "Kroki"}, STEPS.map((name, i) => {
    const n = i + 1, open = n < w.step;
    return h("li", {"aria-current": n === w.step ? "step" : null, class: open ? "done" : null},
      open ? h("button", {type: "button", onclick: () => { w.step = n; draw(); }}, name) : h("span", {}, name));
  })));
  const top = window.scrollY;
  view.replaceChildren(stepper, ...(w.step === 1 ? stepFolder() : w.step === 2 ? stepWhat() : stepStart()).filter(Boolean));
  window.scrollTo(0, top);
  if (w.step === 3 && w.profile === CUSTOM && !w.yaml) guard(loadBase)();
}

export async function show(root, [preset]) {
  view = root;
  meta = await api("/api/meta");
  if (!w || preset) w = fresh(preset || "");
  if (preset) history.replaceState(null, "", "#nowy");   // coming back to "Nowy film" keeps the answers instead of starting over
  if (!w.listing) {
    w.listing = await api("/api/folders?path=");
    draw();
    const last = store.get("folder", "");
    if (last && !w.listing.recent.length) browse(last);
  } else draw();
}
