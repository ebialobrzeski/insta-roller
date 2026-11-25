// Ustawienia rolek i eksportu jako formularz: format, klatki, kodek, jakość albo bitrate, dźwięk, ruch kamery,
// strefa zasłaniana przez aplikacje, dane lotu, długości rolek. What is changed here is kept by the app on top
// of the profile's file (settings.overrides), so the files and their comments stay untouched.
import {h, api, guard, toast} from "./ui.js";

const NAMES = {flight: "Lot szybowcem", flight_motor: "Lot motoszybowcem", holiday: "Urlop"};
const FORMATS = [["1080x1920", "Pionowy 9:16 · 1080 × 1920"], ["2160x3840", "Pionowy 9:16 4K · 2160 × 3840"],
                 ["1080x1350", "Pionowy 4:5 · 1080 × 1350"], ["1080x1080", "Kwadrat · 1080 × 1080"],
                 ["1920x1080", "Poziomy 16:9 · 1920 × 1080"], ["3840x2160", "Poziomy 16:9 4K · 3840 × 2160"]];
// Every setting the form owns, as a path into the profile.
const PATHS = ["output.width", "output.height", "output.fps", "export.codec", "export.quality", "export.bitrate_mbps",
               "export.audio_kbps", "audio", "encoder", "transition_s", "max_turn_dps", "max_track_dps",
               "safe_zone.top", "safe_zone.bottom", "safe_zone.left", "safe_zone.right",
               "overlay.enabled", "overlay.scale", "overlay.bottom", "overlay.speed_max_kmh", "target_length_s", "holiday.trip_length_s"];
const get = (o, path) => path.split(".").reduce((x, k) => (x == null ? undefined : x[k]), o);
function set(o, path, value) {
  const keys = path.split("."), last = keys.pop();
  keys.reduce((x, k) => (x[k] = x[k] && typeof x[k] === "object" ? x[k] : {}), o)[last] = value;
}

let state = null;                                  // {settings, name, form, v (values being edited), lengths}

function changes() {                               // only what differs from the profile's own file is kept
  const out = {};
  for (const path of PATHS) {
    const now = get(state.v, path), base = get(state.form.base, path);
    if (now !== undefined && JSON.stringify(now) !== JSON.stringify(base)) set(out, path, now);
  }
  return out;
}

const field = (label, control, hint) => h("label", {class: "field"}, label, control, hint && h("small", {class: "muted"}, hint));
function slider(path, min, max, step, show, hint, label) {
  const out = h("output", {}, show(get(state.v, path)));
  return field(label, h("div", {class: "slide"},
    h("input", {type: "range", min, max, step, value: get(state.v, path), oninput: e => { set(state.v, path, +e.target.value); out.textContent = show(+e.target.value); }}), out), hint);
}
const number = (path, label, min, max, step, hint, unit) => field(label, h("div", {class: "slide"},
  h("input", {type: "number", min, max, step, inputmode: "decimal", value: get(state.v, path) ?? "", style: "max-width:140px",
              oninput: e => { if (e.target.value !== "") set(state.v, path, +e.target.value); }}), unit && h("span", {class: "muted"}, unit)), hint);
const choice = (path, label, options, hint, cast = x => x) => field(label,
  h("select", {onchange: e => { set(state.v, path, cast(e.target.value)); draw(); }}, options.map(([value, text]) =>
    h("option", {value, selected: String(get(state.v, path)) === String(value)}, text))), hint);
const toggle = (path, title, text) => h("label", {class: "switch"},
  h("input", {type: "checkbox", checked: !!get(state.v, path), onchange: e => { set(state.v, path, e.target.checked); draw(); }}), h("span", {}, title, text && h("small", {}, text)));
const percent = v => `${Math.round(v * 100)}%`;

function exportBlock() {
  const v = state.v, size = `${v.output.width}x${v.output.height}`, known = FORMATS.some(f => f[0] === size);
  const byRate = v.export.bitrate_mbps > 0, pixels = v.output.width * v.output.height;
  const usual = pixels > 5e6 ? "45–80" : "12–20";
  return h("div", {class: "cell"},
    h("h3", {class: "h3"}, "Eksport"),
    field("Format i rozdzielczość",
      h("select", {onchange: e => {
        if (e.target.value === "own") { state.own = true; } else { state.own = false; const [w, hh] = e.target.value.split("x").map(Number); v.output.width = w; v.output.height = hh; }
        draw();
      }}, [...FORMATS, ["own", "Własna…"]].map(([value, text]) => h("option", {value, selected: state.own || !known ? value === "own" : value === size}, text))),
      "Rolki na TikTok i Instagram: pionowy 9:16. 4K trwa około czterech razy dłużej i wymaga jakości „Najlepsza”."),
    (state.own || !known) && h("div", {class: "pair"}, number("output.width", "Szerokość", 320, 7680, 2, null, "px"), number("output.height", "Wysokość", 320, 7680, 2, null, "px")),
    h("div", {class: "pair"},
      choice("output.fps", "Klatki na sekundę", [[24, "24"], [25, "25"], [30, "30 (zwykle)"], [50, "50"], [60, "60"]], "Więcej klatek to płynniejszy ruch i większy plik.", Number),
      choice("export.codec", "Kodek", [["h264", "H.264: otworzy się wszędzie"], ["hevc", "H.265 (HEVC): mniejszy plik"]], "H.265 daje tę samą jakość w mniejszym pliku, ale starsze urządzenia go nie odtworzą.")),
    field("Jak ustalić jakość obrazu", h("div", {class: "seg", role: "radiogroup", style: "max-width:420px"},
      [[false, "Stała jakość"], [true, "Stały bitrate"]].map(([rate, text]) => h("button", {type: "button", role: "radio", "aria-checked": String(byRate === rate),
        onclick: () => { v.export.bitrate_mbps = rate ? (v.export.bitrate_mbps || (pixels > 5e6 ? 60 : 16)) : 0; draw(); }}, text)))),
    byRate ? number("export.bitrate_mbps", "Bitrate obrazu", 1, 200, 1,
        `Zwykle ${usual} Mb/s przy tej rozdzielczości. Minuta filmu zajmie około ${Math.round(v.export.bitrate_mbps * 7.5)} MB.`, "Mb/s")
      : slider("export.quality", 14, 30, 1, q => `${q}${q <= 17 ? " · bardzo wysoka" : q <= 22 ? " · wysoka" : q <= 26 ? " · średnia" : " · niska"}`,
          "Mniejsza liczba to lepszy obraz i większy plik. Bitrate dobiera się sam do treści. 21 to dobry punkt wyjścia.", "Jakość obrazu"),
    h("div", {class: "pair"},
      choice("encoder", "Czym kodować", [["auto", "Automatycznie"], ["gpu", "Karta graficzna (szybko)"], ["cpu", "Procesor (wolniej, nieco lepiej)"]]
        .concat(["auto", "gpu", "cpu"].includes(v.encoder) ? [] : [[v.encoder, v.encoder]])),
      choice("export.audio_kbps", "Bitrate dźwięku", [[96, "96 kb/s"], [128, "128 kb/s"], [160, "160 kb/s (zwykle)"], [192, "192 kb/s"], [256, "256 kb/s"], [320, "320 kb/s"]], null, Number)),
    toggle("audio", "Dźwięk z nagrania", "Wyłączony: rolka jest niema (np. pod muzykę dodaną w telefonie)."));
}

function cameraBlock(flight) {
  return h("div", {class: "cell"},
    h("h3", {class: "h3"}, "Ruch kamery"),
    slider("max_turn_dps", 10, 90, 5, d => `${d}°/s`, "Kamera nigdy nie obraca się szybciej. Pół obrotu przy 30°/s trwa 6 sekund.", "Największa prędkość obrotu"),
    slider("transition_s", 0.5, 4, 0.5, t => `${String(t).replace(".", ",")} s`, "Ile co najmniej trwa przejście między dwoma kadrami w jednym ujęciu.", "Czas przejścia między kadrami"),
    !flight && slider("max_track_dps", 4, 40, 2, d => `${d}°/s`, "Jak szybko kamera podąża za osobą. Wolniej = spokojniej, ale osoba może wyjść z kadru.", "Prędkość podążania za osobą"),
    h("h3", {class: "h3 mt2"}, "Co zasłania TikTok / Instagram"),
    h("p", {class: "muted", style: "font-size:13px;margin:4px 0 8px"}, "Część kadru przykrytą przez przyciski i opisy. Aplikacja trzyma to, co ważne, poza nią."),
    slider("safe_zone.top", 0, 0.3, 0.01, percent, null, "Od góry"), slider("safe_zone.bottom", 0, 0.35, 0.01, percent, null, "Od dołu"),
    slider("safe_zone.left", 0, 0.2, 0.01, percent, null, "Z lewej"), slider("safe_zone.right", 0, 0.25, 0.01, percent, null, "Z prawej"));
}

function reelsBlock(flight) {
  const v = state.v, low = v.overlay.bottom != null;
  return h("div", {class: "cell"},
    h("h3", {class: "h3"}, flight ? "Długość rolek" : "Długość filmów"),
    flight ? h("div", {class: "lengths", style: "margin-top:8px"}, state.form.themes.map(t => h("label", {class: "length", style: "grid-template-columns:1fr 84px auto"}, h("span", {}, t.title),
        h("input", {type: "number", min: 5, max: 600, step: 5, inputmode: "numeric", "aria-label": `Długość rolki ${t.title} w sekundach`, value: state.lengths[t.name] || "", placeholder: String(Math.round(t.length || 30)),
                    oninput: e => { if (e.target.value) state.lengths[t.name] = +e.target.value; else delete state.lengths[t.name]; }}), h("span", {class: "muted"}, "s"))))
      : [number("target_length_s", "Film z jednego dnia", 10, 900, 5, "Gdy w danym dniu jest mniej dobrego materiału, film wyjdzie krótszy.", "s"),
         number("holiday.trip_length_s", "Film z całego wyjazdu", 30, 1800, 10, null, "s")],
    flight && h("h3", {class: "h3 mt2"}, "Dane lotu na obrazie"),
    flight && toggle("overlay.enabled", "Pokazuj prędkość, wysokość i wznoszenie"),
    flight && v.overlay.enabled && slider("overlay.scale", 0.6, 1.6, 0.1, x => `${Math.round(x * 100)}%`, null, "Wielkość"),
    flight && v.overlay.enabled && h("label", {class: "switch"}, h("input", {type: "checkbox", checked: low, onchange: e => { v.overlay.bottom = e.target.checked ? 0.07 : null; draw(); }}),
      h("span", {}, "Własna wysokość położenia", h("small", {}, "Wyłączone: tuż nad strefą zasłanianą przez aplikacje."))),
    flight && v.overlay.enabled && low && slider("overlay.bottom", 0, 0.3, 0.01, percent, "Odstęp od dolnej krawędzi kadru.", "Wysokość nad dołem"),
    flight && v.overlay.enabled && number("overlay.speed_max_kmh", "Koniec skali prędkościomierza", 80, 400, 10, null, "km/h"));
}

function draw() {
  const box = state.box, flight = state.v.kind !== "holiday";
  const top = window.scrollY;
  const dirty = Object.keys(changes()).length > 0;
  box.replaceChildren(
    h("div", {class: "row"}, h("div", {class: "cell"},
      h("div", {class: "spread"},
        h("div", {}, h("h2", {class: "title"}, "Rolki i eksport"), h("p", {class: "sub"}, "Jak mają wyglądać i w jakim formacie się zapisywać. Osobno dla każdego rodzaju nagrań.")),
        h("div", {class: "seg", role: "radiogroup", "aria-label": "Rodzaj nagrań"}, state.names.map(name =>
          h("button", {type: "button", role: "radio", "aria-checked": String(name === state.name), onclick: guard(() => open(name))}, NAMES[name] || name)))))),
    h("div", {class: "row c3"}, exportBlock(), cameraBlock(flight), reelsBlock(flight)),
    h("div", {class: "bar"},
      h("button", {class: "btn", type: "button", disabled: !state.form.changed && !dirty, onclick: guard(async () => {
        if (!confirm(`Przywrócić ustawienia fabryczne dla „${NAMES[state.name] || state.name}”?`)) return;
        await save({}, true);
      })}, "Przywróć fabryczne"),
      h("span", {class: "grow muted", style: "font-size:13px;text-align:right"}, "Zmiany dotyczą rolek robionych od teraz. Gotowych nie zmieniają."),
      h("button", {class: "btn primary", type: "button", onclick: guard(() => save(changes()))}, "Zapisz ustawienia")));
  window.scrollTo(0, top);
}

async function save(diff, reset) {
  const overrides = {...state.settings.overrides, [state.name]: diff};
  const lengths = reset ? Object.fromEntries(Object.entries(state.lengths).filter(([name]) => !state.form.themes.some(t => t.name === name))) : state.lengths;
  Object.assign(state.settings, await api("/api/settings", "PUT", {overrides, lengths}));
  toast(reset ? "Przywrócono ustawienia fabryczne" : "Zapisano ustawienia");
  await open(state.name);
}

async function open(name) {
  state.name = name;
  state.form = await api(`/api/config/profiles/${encodeURIComponent(name)}/form`);
  state.v = JSON.parse(JSON.stringify(state.form.values));
  state.lengths = {...state.settings.lengths};
  state.own = false;
  draw();
}

export async function show(box, settings, meta) {
  const names = meta.profiles.filter(p => !p.error).map(p => p.name);
  state = {box, settings, names, name: names.includes("flight") ? "flight" : names[0]};
  if (state.name) await open(state.name);
}
