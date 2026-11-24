// Nowe rolki: three steps. 1 Nagrania, 2 Rolki, 3 Ujęcia (the review screen, review.js, where the work is started).
import {$, h, api, icon, guard, toast, store, duration, plural, THEMES, inWindow, chooseFolder, flowSteps} from "./ui.js";
import {live, setContext} from "./live.js";

const CUSTOM = "__custom__";
const STEPS = ["Nagrania", "Rolki", "Ujęcia"];
// Seconds of work for one second of film, by quality: honest orders of magnitude, not promises.
const QUALITIES = [
  ["best", "Najlepsza", "Pełna ostrość, bez widocznego łączenia obiektywów. Najdłużej.", 15],
  ["good", "Dobra", "Pełna ostrość, ale na łączeniu obiektywów bywa widoczny szew.", 5],
  ["fast", "Szybki podgląd", "Mała rozdzielczość. Żeby sprawdzić, co wyjdzie, zanim zrobisz rolki na czysto.", 1],
];
let view, meta, w;

function fresh(preset) {
  return {step: 1, preset, listing: null, found: null, checking: false, kind: preset === "lot" ? "flight" : preset === "urlop" ? "holiday" : null,
          themes: null, overlay: null, ai: null, aiModel: "", aiReasoning: "", lengths: {}, counts: {}, mode: "day", length: "", people: null, auto: false, shots: preset === "ujecia", films: preset !== "ujecia",
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
    return {name, title: t.title || title, text, length: +w.lengths[name] || +w.length || t.length || p.length, usual: t.length || p.length, count: Math.min(Math.max(+w.counts[name] || 1, 1), 12), def: !!t.enabled,
            on: w.themes && name in w.themes ? w.themes[name] : !!t.enabled};
  });
}
function filmSeconds() {
  const p = profile() || {};
  if (flight()) return themes().filter(t => t.on).reduce((sum, t) => sum + t.length * t.count, 0);
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
      yes(f.full_quality, "Są pliki w pełnej jakości.", "Nie ma plików w pełnej jakości (.insv): rolki będą mniej ostre."),
      yes(f.previews, "Są małe pliki podglądu: szukanie momentów pójdzie szybko.", "Nie ma małych plików podglądu: szukanie momentów potrwa dłużej."),
      yes(f.igc, "Jest zapis lotu z logera (plik IGC).", "Nie ma zapisu lotu z logera (pliku IGC): użyję danych z samej kamery."),
      l.job && h("li", {}, "Z tych nagrań robiłem już rolki. Znalezione momenty wykorzystam ponownie.")))),
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
      h("p", {class: "sub"}, "Wskaż folder z plikami z kamery: jeden lot albo jeden wyjazd. Niczego w nim nie zmienię, rolki trafią do osobnego podfolderu."))),
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
    bar(null, h("button", {class: "btn primary", type: "button", disabled: !(w.found && w.found.usable), onclick: () => { w.step = 2; draw(); }}, "Dalej: wybierz rolki", icon("right"))),
  ];
}

// --- step 2: which reels ----------------------------------------------------------------------
const pick = (on, title, text, tags, onclick, disabled) => h("button", {class: "cell pick", type: "button", "aria-pressed": String(!!on), onclick, disabled},
  h("span", {class: "tick"}, icon("check")),
  h("div", {class: "h3", style: "padding-right:28px"}, title), h("p", {class: "muted", style: "margin-top:6px;font-size:14px"}, text),
  tags && h("div", {class: "tags", style: "margin-top:12px"}, tags));
const toggle = (key, title, text) => h("label", {class: "switch"},
  h("input", {type: "checkbox", checked: !!w[key], onchange: e => { w[key] = e.target.checked; draw(); }}), h("span", {}, title, h("small", {}, text)));

// Who frames the shots: this film's choice, else the Settings, else the profile.
const aiDefault = () => { const p = profile() || {}, m = meta.ai || {}; return {engine: m.ai_framing || p.ai || "off", model: m.ai_model || p.ai_model || "", reasoning: m.ai_reasoning || p.ai_reasoning || 0}; };
const aiEngine = () => w.ai || aiDefault().engine;
let aiModels;                                   // undefined = not asked, null = loading, [] = no list
async function loadModels() {
  if (aiModels !== undefined || aiEngine() !== "openrouter") return;
  aiModels = null;
  try { aiModels = (await api("/api/ai/models")).models || []; } catch { aiModels = []; }
  if (w && w.step === 2) draw();
}

// One kind of reel: a switch, and when it is on, how many and how long.
function reelTile(t) {
  const flip = () => { w.themes = {...Object.fromEntries(themes().map(x => [x.name, x.on])), [t.name]: !t.on}; draw(); };
  const count = n => { w.counts[t.name] = Math.min(Math.max(n, 1), 12); draw(); };
  return h("div", {class: `cell reel${t.on ? " on" : ""}`},
    h("button", {class: "reel-pick", type: "button", "aria-pressed": String(t.on), onclick: flip},
      h("span", {class: "tick"}, icon("check")),
      h("span", {class: "h3"}, t.title), h("span", {class: "muted about"}, t.text)),
    t.on && h("div", {class: "reel-opts"},
      h("div", {class: "stepper-n", role: "group", "aria-label": `Ile rolek: ${t.title}`},
        h("button", {class: "btn", type: "button", "aria-label": "O jedną mniej", disabled: t.count <= 1, onclick: () => count(t.count - 1)}, "−"),
        h("b", {}, plural(t.count, "rolka", "rolki", "rolek")),
        h("button", {class: "btn", type: "button", "aria-label": "O jedną więcej", disabled: t.count >= 12, onclick: () => count(t.count + 1)}, "+")),
      h("label", {class: "secs"}, "po",
        h("input", {type: "number", min: 5, max: 600, step: 5, inputmode: "numeric", "aria-label": `Długość rolki ${t.title} w sekundach`,
          value: w.lengths[t.name] || "", placeholder: String(Math.round(+w.length || t.usual)),
          oninput: e => { w.lengths[t.name] = e.target.value; }, onchange: () => draw()}), "s")),
    !t.on && !t.def && h("div", {class: "reel-opts muted", style: "font-size:12px"}, "Zwykle wyłączona"));
}

function summary() {
  const film = filmSeconds();
  if (!w.films) return "Powstaną osobne ujęcia do własnego montażu, bez gotowych rolek.";
  if (flight()) {
    const on = themes().filter(t => t.on), n = on.reduce((sum, t) => sum + t.count, 0);
    return `${plural(n, "rolka", "rolki", "rolek")}, razem około ${Math.round(film)} s materiału.`;
  }
  return {day: "Jeden film z każdego dnia wyjazdu.", trip: "Jeden film z całego wyjazdu.", both: "Film z każdego dnia i jeden z całego wyjazdu."}[w.mode];
}

// Everything that is usually right as it is: closed, one line says what is set.
function settingsBlock() {
  const p = profile() || {}, q = quality(), ai = aiDefault(), engine = aiEngine();
  const names = meta.profiles.filter(x => !x.error).map(x => x.name), custom = w.profile === CUSTOM;
  const opt = (value, text, chosen) => h("option", {value, selected: value === chosen}, text);
  const overlayOn = w.overlay == null ? !!p.overlay : w.overlay;
  const qName = QUALITIES.find(x => x[0] === q.value)[1];
  const aiName = {off: "stałe kadry", local: "AI na tym komputerze", openrouter: "AI przez internet"}[engine];
  const chosenModel = (aiModels || []).find(m => m.id === (w.aiModel || ai.model));
  const line = [`jakość: ${qName.toLowerCase()}`, `kadry: ${aiName}`, flight() ? `dane lotu na obrazie: ${overlayOn ? "tak" : "nie"}` : null,
                w.auto ? "bez sprawdzania ujęć" : null].filter(Boolean).join(" · ");
  return h("details", {open: w.more, ontoggle: e => { w.more = e.target.open; if (w.more) loadModels(); }},
    h("summary", {}, "Ustawienia tych rolek", h("span", {class: "muted", style: "font-weight:400"}, ` (${line})`)),
    h("p", {class: "muted", style: "font-size:13px;margin:8px 0 0"}, "Zwykle nie trzeba tu nic zmieniać. To, co wybierasz za każdym razem tak samo, ustaw raz w ",
      h("a", {href: "#ustawienia"}, "Ustawieniach"), "."),
    h("div", {class: "row c2", style: "margin-top:12px"},
      h("div", {class: "cell tight"},
        h("label", {class: "field"}, "Jakość rolek",
          h("select", {onchange: e => { w.quality = e.target.value; draw(); }}, QUALITIES.map(([value, title, text]) =>
            h("option", {value, selected: q.value === value, disabled: !q.can[value]}, `${title}: ${text}${q.can[value] ? "" : " (niedostępna)"}`))),
          h("small", {class: "muted"}, "Zmienisz ją jeszcze tuż przed robieniem rolek, gdy zobaczysz ujęcia.")),
        flight() && h("label", {class: "switch mt"}, h("input", {type: "checkbox", checked: overlayOn, onchange: e => { w.overlay = e.target.checked; draw(); }}),
          h("span", {}, "Dane lotu na obrazie", h("small", {}, "Prędkość, wysokość i wznoszenie w rogu kadru."))),
        w.films ? toggle("shots", "Zapisz też osobne ujęcia", "Każde ujęcie dodatkowo jako oddzielny plik, do własnego montażu.")
          : toggle("films", "Zrób też gotowe rolki", "Oprócz osobnych ujęć sklej je w rolki."),
        toggle("auto", "Zrób od razu, bez sprawdzania ujęć", "Aplikacja nie zatrzyma się, żeby pokazać wybrane ujęcia. Rolki powstaną w ciemno.")),
      h("div", {class: "cell tight"},
        h("label", {class: "field"}, "Kto wybiera kadry",
          h("select", {onchange: e => { w.ai = e.target.value; loadModels(); draw(); }},
            [["off", "Stałe kadry, zawsze takie same dla danego rodzaju rolki"], ["local", "AI na tym komputerze: nic nie wychodzi do internetu"],
             ["openrouter", "AI przez internet (OpenRouter): najlepszy wybór, płatne"]]
              .map(([value, text]) => h("option", {value, selected: value === engine}, text))),
          h("small", {class: "muted"}, engine === "openrouter" ? "AI ogląda każde ujęcie dookoła i samo ustawia kamerę. Małe klatki z nagrania są wysyłane do OpenRouter."
            : engine === "local" ? "Model na tym komputerze wybiera najładniejszy z gotowych kadrów. Nie rozumie sceny tak jak AI przez internet."
            : "Kamera patrzy zawsze tam, gdzie przewiduje rodzaj rolki.")),
        engine === "openrouter" && h("label", {class: "field mt"}, "Model AI",
          h("input", {type: "text", list: "ai-models", value: w.aiModel, placeholder: ai.model, autocomplete: "off", spellcheck: "false",
            oninput: e => { w.aiModel = e.target.value.trim(); }, onchange: () => draw()}),
          h("datalist", {id: "ai-models"}, (aiModels || []).map(m => h("option", {value: m.id}, `${m.name}${m.reasoning ? " · z namysłem" : ""} · $${m.prompt_usd_per_m} za mln tokenów`))),
          h("small", {class: "muted"}, aiModels === null ? "Wczytuję listę modeli…" : aiModels && aiModels.length ? `Puste pole: ${ai.model}. Zacznij pisać, żeby wybrać inny.` : "Wpisz nazwę modelu, np. anthropic/claude-sonnet-5.5.")),
        engine === "openrouter" && h("label", {class: "field mt"}, "Ile AI może się namyślać (tokeny)",
          h("input", {type: "number", min: 0, max: 200000, step: 1000, inputmode: "numeric", value: w.aiReasoning, placeholder: String(ai.reasoning),
            oninput: e => { w.aiReasoning = e.target.value; }}),
          h("small", {class: "muted"}, chosenModel && !chosenModel.reasoning ? "Ten model nie potrafi się namyślać: liczba zostanie pominięta."
            : "0 = odpowiada od razu. Więcej = lepiej przemyślane rolki, ale drożej i dłużej.")))),
    h("div", {class: "row c3", style: "margin-top:12px"},
      h("div", {class: "cell tight"}, h("label", {class: "field"}, "Gdzie zapisać rolki",
        h("input", {class: "mono", type: "text", value: w.out, autocomplete: "off", spellcheck: "false", placeholder: w.found ? w.found.out : "", oninput: e => { w.out = e.target.value; }}),
        h("small", {class: "muted"}, "Puste pole: podfolder obok nagrań.")),
        inWindow && h("button", {class: "btn small", type: "button", style: "margin-top:8px", onclick: guard(async () => {
          const path = await chooseFolder(w.out || w.listing.path);
          if (path) { w.out = path; draw(); }
        })}, icon("folder"), "Wybierz inny folder…")),
      h("div", {class: "cell tight"}, h("label", {class: "field"}, "Gdzie była zamocowana kamera",
        h("select", {onchange: e => { w.mount = e.target.value; }}, opt("", "Jak zwykle dla tego rodzaju nagrań", w.mount), meta.mounts.map(m => opt(m.name, m.name, w.mount))))),
      h("div", {class: "cell tight"}, h("label", {class: "field"}, "Zestaw ustawień",
        h("select", {onchange: e => { w.profile = e.target.value; w.themes = null; draw(); }},
          opt("auto", "Dobierz sam", w.profile), names.map(n => opt(n, n, w.profile)), opt(CUSTOM, "Własny, tylko dla tych rolek", w.profile))))),
    custom && h("div", {class: "row"}, h("div", {class: "cell"},
      h("div", {class: "h3"}, "Własny zestaw ustawień"),
      h("p", {class: "muted", style: "font-size:13px"}, "Dla zaawansowanych: pełny plik ustawień, który można dowolnie zmieniać."),
      h("div", {class: "inline mt"},
        h("select", {id: "c-base", "aria-label": "Na podstawie zestawu", onchange: guard(loadBase)}, names.map(n => opt(n, `Na podstawie: ${n}`))),
        h("select", {id: "c-format", "aria-label": "Format"}, opt("", "Format bez zmiany"), opt("1080x1920", "Pionowy 9:16"), opt("1920x1080", "Poziomy 16:9"), opt("1080x1080", "Kwadrat 1:1")),
        h("select", {id: "c-audio", "aria-label": "Dźwięk"}, opt("", "Dźwięk bez zmiany"), opt("1", "Dźwięk z nagrania"), opt("0", "Bez dźwięku")),
        h("button", {class: "btn", type: "button", onclick: guard(compose)}, "Wstaw do ustawień")),
      h("label", {class: "field mt"}, "Wszystkie ustawienia (YAML)",
        h("textarea", {id: "c-yaml", rows: 14, spellcheck: "false", value: w.yaml, oninput: e => { w.yaml = e.target.value; }})),
      h("div", {class: "inline mt"},
        h("button", {class: "btn", type: "button", onclick: guard(async () => { await api("/api/config/profiles/validate", "POST", {text: w.yaml}); toast("Ustawienia są poprawne"); })}, "Sprawdź"),
        h("input", {id: "c-name", type: "text", placeholder: "nazwa_nowego_zestawu", autocomplete: "off", "aria-label": "Nazwa nowego zestawu"}),
        h("button", {class: "btn", type: "button", onclick: guard(saveCustom)}, "Zapisz jako nowy zestaw")))));
}

function stepWhat() {
  const p = profile() || {}, rows = [];
  rows.push(h("div", {class: "row"}, h("div", {class: "cell"}, h("h1", {class: "title"}, flight() ? "Jakie rolki zrobić?" : "Jakie filmy zrobić?"),
    h("p", {class: "sub"}, flight() ? "Zaznacz rodzaje rolek. Z długiego lotu możesz zrobić po kilka rolek każdego rodzaju: każda weźmie inne momenty."
      : "Wybierz, jak podzielić nagrania z wyjazdu."))));
  if (flight()) {
    rows.push(h("div", {class: "row c3"}, themes().map(reelTile)));
  } else {
    const mine = w.people || p.people || [];
    rows.push(h("div", {class: "row c3"}, [["day", "Film na każdy dzień", "Osobny film z każdego dnia wyjazdu."], ["trip", "Jeden film z wyjazdu", "Wszystkie dni w jednym filmie."],
      ["both", "Jedno i drugie", "Film z każdego dnia i dodatkowo jeden z całości."]].map(([value, title, text]) => pick(w.mode === value, title, text, null, () => { w.mode = value; draw(); }))));
    rows.push(h("div", {class: "row c2"},
      h("div", {class: "cell"}, h("label", {class: "field"}, "Długość filmu z jednego dnia (sekundy)",
        h("input", {type: "number", min: 5, step: 5, inputmode: "numeric", value: w.length, placeholder: `${p.length || 120} (zwykle)`, oninput: e => { w.length = e.target.value; }}),
        h("small", {class: "muted"}, "Gdy w danym dniu jest mniej dobrego materiału, film wyjdzie krótszy."))),
      h("div", {class: "cell"}, h("div", {class: "field"}, "Kogo szukać w nagraniach"),
        meta.people.length ? h("div", {class: "checks", style: "margin-top:8px"}, meta.people.map(name => h("label", {class: "check"},
            h("input", {type: "checkbox", checked: mine.includes(name), onchange: e => { const set = new Set(w.people || p.people || []); e.target.checked ? set.add(name) : set.delete(name); w.people = [...set]; }}), name)))
          : h("p", {class: "muted", style: "font-size:14px;margin-top:6px"}, "Lista osób jest pusta. Dodasz je w zakładce „Osoby”."))));
  }
  rows.push(h("div", {class: "row"}, h("div", {class: "cell"}, settingsBlock())));
  const nothing = flight() && !themes().some(t => t.on);
  rows.push(bar(h("button", {class: "btn", type: "button", onclick: () => { w.step = 1; draw(); }}, "Wstecz"),
    h("button", {class: "btn primary big", type: "button", disabled: nothing, onclick: guard(submit)}, w.auto ? icon("film") : icon("eye"),
      w.auto ? "Zrób od razu" : "Znajdź najlepsze momenty"),
    nothing ? "Zaznacz przynajmniej jeden rodzaj rolki."
      : `${summary()} ${w.auto ? "Rolki powstaną bez pytania o ujęcia." : "Szukanie potrwa parę minut. Potem pokażę wybrane ujęcia, zanim cokolwiek zacznie się robić."}`));
  return rows;
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

async function submit() {
  const p = profile() || {}, custom = w.profile === CUSTOM, list = themes(), ai = aiDefault(), engine = aiEngine();
  const people = w.people && !flight() && JSON.stringify([...w.people].sort()) !== JSON.stringify([...(p.people || [])].sort()) ? w.people : [];
  const job = await api("/api/jobs", "POST", {folder: w.listing.path, options: {
    out: w.out.trim(), profile: custom ? "auto" : profileName(), profile_yaml: custom ? w.yaml : "", mount: w.mount,
    length: +w.length || null,
    counts: flight() ? Object.fromEntries(list.filter(t => t.on && t.count > 1).map(t => [t.name, t.count])) : {},
    lengths: flight() ? Object.fromEntries(list.filter(t => t.on && +w.lengths[t.name] >= 5 && +w.lengths[t.name] !== t.usual).map(t => [t.name, +w.lengths[t.name]])) : {},
    enable: flight() ? list.filter(t => t.on && !t.def).map(t => t.name) : [],
    disable: flight() ? list.filter(t => !t.on && t.def).map(t => t.name) : [],
    mode: flight() ? "day" : w.mode, people,
    overlay: w.overlay == null || w.overlay === !!p.overlay ? null : w.overlay,
    ai_framing: w.ai && w.ai !== ai.engine ? w.ai : null,
    ai_model: engine === "openrouter" && w.aiModel && w.aiModel !== ai.model ? w.aiModel : "",
    ai_reasoning: engine === "openrouter" && w.aiReasoning !== "" && +w.aiReasoning !== ai.reasoning ? +w.aiReasoning : null,
    review: !w.auto, shots: w.shots, films: w.films, quality: quality().value,
  }});
  const auto = w.auto;
  toast(job.existing && job.status === "running" ? "Te nagrania są właśnie w robocie, nic nie zmieniłem" : auto ? "Zaczynam. Postęp zobaczysz tutaj." : "Szukam najlepszych momentów.");
  w = null;
  location.hash = auto ? `#filmy/${job.id}` : `#filmy/${job.id}/ujecia`;
}

// --- the frame of every step ------------------------------------------------------------------
const bar = (back, next, note) => h("div", {class: "bar"}, back, h("span", {class: "grow muted", style: "font-size:13px;text-align:right"}, note || ""), next);

function draw() {
  if (!w || !view.isConnected) return;
  setContext("Nowe rolki", STEPS[w.step - 1]);
  const top = window.scrollY;
  view.replaceChildren(flowSteps(w.step, n => { w.step = n; draw(); }), ...(w.step === 1 ? stepFolder() : stepWhat()).filter(Boolean));
  window.scrollTo(0, top);
  if (w.step === 2 && w.profile === CUSTOM && !w.yaml && $("#c-base")) guard(loadBase)();
}

export async function show(root, [preset, from]) {
  view = root;
  meta = await api("/api/meta");
  const again = preset === "z" && live.jobs.find(j => j.id === +from);      // other reels from recordings already known
  if (again) w = fresh(again.kind === "holiday" ? "urlop" : "lot");
  else if (!w || preset) w = fresh(preset || "");
  if (preset) history.replaceState(null, "", "#nowy");   // coming back to "Nowe rolki" keeps the answers instead of starting over
  if (!w.listing) {
    w.listing = await api("/api/folders?path=");
    draw();
    const last = store.get("folder", "");
    if (again) { await browse(again.folder); if (w && w.found && w.found.usable) { w.step = 2; draw(); } }
    else if (last && !w.listing.recent.length) browse(last);
  } else draw();
}
