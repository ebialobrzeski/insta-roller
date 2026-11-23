// Shared pieces of the UI: DOM helper, API calls, icons, and the words used for things.

export const $ = (sel, el = document) => el.querySelector(sel);

const SVG_NS = "http://www.w3.org/2000/svg";
const SVG_TAGS = new Set(["svg", "path", "circle", "rect", "line", "polyline", "polygon", "text", "g", "defs", "linearGradient", "stop"]);

export function h(tag, attrs, ...kids) {
  const el = SVG_TAGS.has(tag) ? document.createElementNS(SVG_NS, tag) : document.createElement(tag);
  const props = {};
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "value" || key === "checked" || key === "open") props[key] = value;
    else if (value === true) el.setAttribute(key, "");
    else if (value !== false && value != null) el.setAttribute(key, value);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) el.append(kid);
  Object.assign(el, props);            // after the options exist, so a select takes its value
  return el;
}

export async function api(path, method = "GET", body) {
  const response = await fetch(path, {method, headers: body ? {"Content-Type": "application/json"} : {},
                                      body: body ? JSON.stringify(body) : undefined});
  if (response.status === 204) return null;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    throw new Error(typeof detail === "string" ? detail
      : Array.isArray(detail) ? detail.map(d => `${(d.loc || []).join(".")}: ${d.msg}`).join("\n") : response.statusText);
  }
  return data;
}

let toastTimer;
export function toast(text, bad = false) {
  const el = $("#toast");
  el.textContent = text;
  el.className = bad ? "bad" : "";
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 9000 : 3000);
}
export const guard = fn => async (...args) => { try { await fn(...args); } catch (error) { toast(error.message, true); } };

export const store = {
  get(key, fallback = null) { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch (error) { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch (error) { /* a convenience only */ } },
};

// --- icons: thin line drawings, 24 px box -------------------------------------------
const ICONS = {
  home: "M4 10.5 12 4l8 6.5V20h-5v-6H9v6H4z",
  plus: "M12 5v14M5 12h14",
  film: "M4 5h16v14H4zM8 5v14M16 5v14M4 9.5h4M4 14.5h4M16 9.5h4M16 14.5h4",
  people: "M9 11a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM2.5 20c.6-3.4 3.2-5.5 6.5-5.5s5.9 2.1 6.5 5.5M16 4.3a3.5 3.5 0 0 1 0 6.4M18.5 14.9c1.6.9 2.7 2.6 3 5.1",
  gear: "M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zM19 12a7 7 0 0 0-.1-1.3l2-1.5-2-3.4-2.3.9a7 7 0 0 0-2.2-1.3L14 3h-4l-.4 2.4a7 7 0 0 0-2.2 1.3l-2.3-.9-2 3.4 2 1.5a7 7 0 0 0 0 2.6l-2 1.5 2 3.4 2.3-.9a7 7 0 0 0 2.2 1.3L10 21h4l.4-2.4a7 7 0 0 0 2.2-1.3l2.3.9 2-3.4-2-1.5c.1-.4.1-.9.1-1.3z",
  left: "M14.5 5.5 8 12l6.5 6.5",
  right: "M5 12h14M13 6l6 6-6 6",
  menu: "M4 7h16M4 12h16M4 17h16",
  sun: "M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4",
  moon: "M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z",
  screen: "M3.5 5h17v11h-17zM9 20h6M12 16v4",
  folder: "M3.5 6.5h6l2 2.5h9v10h-17z",
  play: "M8 5.5v13l11-6.5z",
  down: "M12 4v11M7 11l5 5 5-5M5 20h14",
  check: "M5 12.5l4.5 4.5L19 7.5",
  alert: "M12 4 2.5 20h19zM12 10v5M12 17.5v.5",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 11v6M12 7.5v.5",
  eye: "M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  glider: "M2.5 13.5 12 11l9.5 2.5M12 6.5V11M12 11c-.9 2.4-.9 4.6 0 7M9.5 18h5",
  beach: "M4 20h16M12 20V9M12 9c-3-3.5-7-2.5-8.5.5 3-1.2 6-.9 8.500-.5zM12 9c3-3.500 7-2.500 8.500.5-3-1.200-6-.9-8.500-.5zM12 9V5",
  cuts: "M4 6h7v5H4zM13 6h7v5h-7zM4 13h7v5H4zM13 13h7v5h-7z",
  sliders: "M4 7h10M18 7h2M4 12h3M11 12h9M4 17h12M20 17h0M16 5v4M9 10v4M18 15v4",
  refresh: "M20 11a8 8 0 0 0-14.500-4.200L4 8.500M4 4v4.500h4.500M4 13a8 8 0 0 0 14.500 4.200L20 15.500M20 20v-4.500h-4.500",
  x: "M6 6l12 12M18 6 6 18",
};
export const icon = (name, cls = "icon") => h("svg", {class: cls, viewBox: "0 0 24 24", "aria-hidden": "true"}, h("path", {d: ICONS[name] || ICONS.info}));

// The product's mark: a glider above the horizon.
export const mark = () => h("svg", {viewBox: "0 0 28 28", fill: "none", stroke: "currentColor", "stroke-width": "1.8", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true"},
  h("path", {d: "M3 12.500 14 9l11 3.500M14 4.500V9M14 9c-.8 2-.8 4 0 6M12 15h4"}),
  h("path", {d: "M3 22c3.500-2 7-2 11 0s7.500 2 11 0", opacity: ".55"}));

// --- words -----------------------------------------------------------------------------
export const seconds = s => `${Math.round(s)} s`;
export const clock = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
export const size = bytes => bytes > 1e9 ? `${(bytes / 1e9).toFixed(1).replace(".", ",")} GB` : `${Math.round(bytes / 1e6)} MB`;
export const comma = (n, digits = 1) => Number(n).toFixed(digits).replace(".", ",");
export const date = t => new Date(t * 1000).toLocaleDateString("pl-PL", {day: "numeric", month: "short", year: "numeric"});
export const dateTime = t => new Date(t * 1000).toLocaleString("pl-PL", {day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"});
export function duration(s) {                 // 8617 -> "2 godz. 24 min"
  s = Math.round(s);
  if (s < 90) return `${s} s`;
  const hours = Math.floor(s / 3600), minutes = Math.round((s % 3600) / 60);
  return hours ? `${hours} godz. ${minutes} min` : `${minutes} min`;
}
export function plural(n, one, few, many) {    // 1 nagranie, 2 nagrania, 5 nagrań
  const mod10 = n % 10, mod100 = n % 100;
  return `${n} ${n === 1 ? one : mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14) ? few : many}`;
}
export const fileUrl = (job, path, download) =>
  `/api/jobs/${job}/files/${path.split("/").map(encodeURIComponent).join("/")}${download ? "?download=1" : ""}`;

// Reel themes: what each film is, in a line.
export const THEMES = {
  takeoff_tow: ["Start i hol", "Rozbieg, wznoszenie za samolotem i wyczepienie."],
  thermal: ["Komin", "Krążenie w noszeniu: skrzydło, ziemia pod spodem, pilot."],
  glide_views: ["Przelot i widoki", "Lot po prostej między kominami, horyzont i krajobraz."],
  flight_summary: ["Skrót lotu", "Cały lot w jednym filmie, po kawałku z każdej części."],
  landing: ["Lądowanie", "Krąg i przyziemienie. Potrzebne nagranie do samego lądowania."],
  holiday_day: ["Dzień", "Film z każdego dnia, ujęcia w kolejności nagrania."],
};

// Shots in human words, from the scene type and what was measured in it.
const TYPES = {takeoff: "Start", tow: "Hol", release: "Wyczepienie", climb: "Wznoszenie", engine_off: "Wyłączenie silnika",
               thermal: "Komin", glide: "Przelot", circuit: "Krąg", landing: "Lądowanie", ending: "Zakończenie",
               view: "Widok", walk: "Spacer", holder: "Ty w kadrze", companion: "Towarzysz", panorama: "Panorama"};
export function shotName(type, attrs = {}) {
  const base = TYPES[type] || type;
  const more = [];
  if (type === "thermal") {
    if (attrs.direction) more.push(attrs.direction === "left" ? "w lewo" : "w prawo");
    if (attrs.climb_ms != null) more.push(`noszenie ${comma(attrs.climb_ms)} m/s`);
  } else if (type === "glide" && attrs.speed_kmh != null) more.push(`${Math.round(attrs.speed_kmh)} km/h`);
  else if (type === "tow" && attrs.alt_gain_m != null) more.push(`${Math.round(attrs.alt_gain_m)} m w górę`);
  else if (type === "release" && attrs.alt_m != null) more.push(`na ${Math.round(attrs.alt_m)} m`);
  if (attrs.person) more.push(String(attrs.person));
  return more.length ? `${base} ${more[0]}${more[1] ? `, ${more[1]}` : ""}` : base;
}

// Camera directions (framing presets) in human words; an unknown preset keeps its id.
const PRESETS = {front: "przód", front_level: "przód, równy horyzont", pilot: "pilot", pilot_wide: "pilot szeroko",
                 back_left: "tył w lewo", back_right: "tył w prawo", wing_left: "skrzydło lewe", wing_right: "skrzydło prawe",
                 instruments: "przyrządy", left_seat: "lewy fotel", right_seat: "prawy fotel", crew: "załoga",
                 view: "widok", holder: "ty", companion: "towarzysz", panorama: "panorama"};
export const presetName = id => PRESETS[id] || id;

// A job's state in words. `short` is for the sidebar.
const PHASES = ["Szukam scen", "Układam plan", "Renderuję"];
export function stateText(job) {
  if (job.status === "queued") return "Czeka w kolejce";
  if (job.status === "review") return "Czeka na przejrzenie ujęć";
  if (job.status === "done") return "Gotowe";
  if (job.status === "error") return "Nie udało się";
  if (job.status === "cancelled") return "Zatrzymane";
  if (job.cancel) return "Zatrzymuję…";
  if (job.stage === "thumbnails") return job.detail ? `Robię miniatury: ${job.detail.shot} z ${job.detail.of}` : "Robię miniatury…";
  if (job.stage === "shots") return job.detail ? `Zapisuję ujęcie ${job.detail.shot} z ${job.detail.of}` : "Zapisuję osobne ujęcia…";
  if (job.stage === "render") return job.detail ? `Renderuję ujęcie ${job.detail.shot} z ${job.detail.of}` : "Renderuję…";
  return `${PHASES[job.phase] || "Pracuję"}…`;
}

// What went wrong, as a sentence and a next step; the raw text stays under "Szczegóły techniczne".
export function problemText(job) {
  const raw = job.error || "", stage = raw.split(":")[0];
  const what = {analyze: "Nie udało się znaleźć scen w nagraniach.", plan: "Nie udało się ułożyć planu filmu.",
                render: "Nie udało się wyrenderować filmu.", shots: "Nie udało się zapisać osobnych ujęć."}[stage] || "Coś poszło nie tak.";
  const hints = [
    [/No camera files/i, "W tym folderze nie ma nagrań z kamery (.insv, .lrv) ani gotowych plików .mp4. Sprawdź, czy wybrany jest właściwy folder."],
    [/No reel could be planned/i, "W nagraniu nie ma scen pasujących do wybranych tematów. Wybierz inne tematy albo „Skrót lotu”."],
    [/needs the stitcher/i, "Najlepsza jakość wymaga programu do sklejania Media SDK, którego tu nie ma. Wybierz jakość „Dobra”."],
    [/no creation time|--video-start/i, "Nagranie nie ma zapisanej godziny, więc nie da się go dopasować do zapisu lotu."],
    [/No IGC file, no GPS/i, "Nie ma zapisu lotu (IGC) ani danych z kamery, po których dałoby się znaleźć sceny."],
    [/FFmpeg not found/i, "Na tym komputerze nie ma programu FFmpeg, którego aplikacja używa do obróbki obrazu."],
    [/No space left|not enough space|disk full/i, "Na dysku zabrakło miejsca. Zwolnij miejsce albo zmień folder pamięci podręcznej w ustawieniach."],
  ];
  const hint = (hints.find(([pattern]) => pattern.test(raw + (job.log || ""))) || [])[1]
    || "Spróbuj ponownie. Jeśli błąd wraca, szczegóły techniczne poniżej pokażą, co dokładnie się stało.";
  return [what, hint];
}
