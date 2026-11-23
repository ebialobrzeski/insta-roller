// Przegląd ujęć: each film as a strip of shots in order. Remove, shorten, lengthen, change the camera
// direction, move between films, add unused scenes; then render.
import {$, h, api, icon, guard, toast, seconds, clock, comma, shotName, presetName, fileUrl, THEMES} from "./ui.js";
import {refresh, setContext} from "./live.js";

const rv = {id: 0, data: null, reels: {}, dirty: new Set(), zones: false, view: null};
const REASONS = {"theme is switched off": "ten temat jest wyłączony", "no usable material": "za mało pasujących scen"};

const sceneOf = id => rv.data.scenes.find(s => s.id === id);
const filmLength = shots => shots.reduce((sum, s) => sum + s.duration_s / (s.speed || 1), 0);
const reelTitle = reel => (reel.plan && reel.plan.title) || (THEMES[reel.theme] || [])[0] || reel.theme;
function touch(tag) { rv.dirty.add(tag); draw(); }

function thumb(scene) {
  const zone = rv.data.safe_zone || {}, [w, hgt] = rv.data.aspect, pct = v => `${(v || 0) * 100}%`;
  return h("div", {class: "thumb", style: `aspect-ratio:${w}/${hgt}`},
    scene && scene.thumb && h("img", {loading: "lazy", alt: "", src: fileUrl(rv.id, `review/${scene.id}.jpg`)}),
    h("i", {style: `top:0;left:0;right:0;height:${pct(zone.top)}`}), h("i", {style: `bottom:0;left:0;right:0;height:${pct(zone.bottom)}`}),
    h("i", {style: `top:0;bottom:0;left:0;width:${pct(zone.left)}`}), h("i", {style: `top:0;bottom:0;right:0;width:${pct(zone.right)}`}));
}

function resize(shot, tag, delta) {              // delta in film seconds; the shot grows around its middle
  const scene = sceneOf(shot.scene_id), speed = shot.speed || 1;
  const length = Math.min(Math.max(shot.duration_s + delta * speed, speed), scene.end_s - scene.start_s);
  const start = Math.min(Math.max(shot.start_s - (length - shot.duration_s) / 2, scene.start_s), scene.end_s - length);
  shot.duration_s = Math.round(length * 100) / 100;
  shot.start_s = Math.round(start * 100) / 100;
  touch(tag);
}

function place(tag, shot) {                      // film order: by time, the ending shot stays last
  const key = s => [s.type === "ending" ? 1 : 0, s.recording || "", s.start_s];
  rv.reels[tag].push(shot);
  rv.reels[tag].sort((a, b) => { const x = key(a), y = key(b); return x[0] - y[0] || x[1].localeCompare(y[1]) || x[2] - y[2]; });
  touch(tag);
}

function shotCard(tag, shot, index) {
  const scene = sceneOf(shot.scene_id) || {}, shots = rv.reels[tag], others = rv.data.reels.filter(r => rv.reels[r.tag] && r.tag !== tag);
  const presets = [...new Set([...rv.data.presets, ...shot.framing])];
  const film = shot.duration_s / (shot.speed || 1);
  return h("div", {class: "shot"},
    h("div", {class: "spread", style: "flex-wrap:nowrap"},
      h("span", {class: "no"}, `${index + 1} · ${clock(shot.start_s)}${shot.speed && shot.speed !== 1 ? ` · ×${shot.speed}` : ""}`),
      h("button", {class: "btn small quiet danger", type: "button", onclick: () => { shots.splice(index, 1); touch(tag); }}, "Usuń")),
    thumb(scene),
    h("div", {class: "what"}, shotName(shot.type, {...scene.attrs, ...shot.attrs})),
    h("div", {class: "len"},
      h("button", {class: "btn", type: "button", "aria-label": "Krócej o pół sekundy", title: "Krócej", onclick: () => resize(shot, tag, -0.5)}, "−"),
      h("b", {}, `${comma(film)} s`),
      h("button", {class: "btn", type: "button", "aria-label": "Dłużej o pół sekundy", title: "Dłużej", onclick: () => resize(shot, tag, 0.5)}, "+")),
    h("div", {}, h("div", {class: "muted", style: "font-size:12px;margin-bottom:4px"}, shot.framing.length > 1 ? "Kadr: kamera przechodzi kolejno" : "Kadr"),
      h("div", {class: "chips"},
        shot.framing.map((name, i) => h("span", {class: "chip"},
          h("select", {value: name, "aria-label": `Kadr ${i + 1}`, onchange: e => { shot.framing[i] = e.target.value; touch(tag); }}, presets.map(p => h("option", {value: p}, presetName(p)))),
          shot.framing.length > 1 && h("button", {type: "button", "aria-label": "Usuń ten kadr", onclick: () => { shot.framing.splice(i, 1); touch(tag); }}, "×"))),
        h("button", {class: "chip add", type: "button", onclick: () => { shot.framing.push(shot.framing[shot.framing.length - 1] || presets[0]); touch(tag); }}, "+ kadr"))),
    others.length > 0 && h("select", {class: "move", "aria-label": "Przenieś do innego filmu", onchange: e => {
      if (!e.target.value) return;
      shots.splice(index, 1);
      rv.dirty.add(tag);
      place(e.target.value, shot);
    }}, h("option", {value: ""}, "Przenieś do…"), others.map(r => h("option", {value: r.tag}, reelTitle(r)))));
}

function reelRow(reel) {
  const shots = rv.reels[reel.tag];
  if (!shots) return h("div", {class: "row"}, h("div", {class: "cell tight muted"}, `Bez filmu „${reelTitle(reel)}”: ${REASONS[reel.reason] || reel.reason}.`));
  const now = filmLength(shots), target = reel.plan.target_length_s, over = now > target * 1.15;
  return h("div", {class: "row"}, h("div", {class: "cell flush"},
    h("div", {style: "padding:24px 32px 16px"},
      h("div", {class: "spread"},
        h("h2", {class: "title"}, reelTitle(reel), rv.dirty.has(reel.tag) && h("span", {class: "live"}, "[ ZMIENIONE ]")),
        h("div", {class: "mono"}, h("b", {style: "font-weight:500"}, seconds(now)), h("span", {class: "muted"}, ` z ${seconds(target)}`))),
      h("div", {class: `lenbar${over ? " over" : ""}`}, h("i", {style: `width:${Math.min(now / target * 100, 100)}%`})),
      over && h("p", {class: "muted", style: "font-size:13px;margin-top:8px"}, "Film jest dłuższy, niż planowano. To nie błąd, ale możesz skrócić ujęcia.")),
    shots.length ? h("div", {class: "strip", tabindex: 0, "aria-label": `Ujęcia filmu ${reelTitle(reel)}, po kolei`}, shots.map((shot, i) => shotCard(reel.tag, shot, i)))
      : h("p", {class: "problem", style: "margin:0 32px 24px"}, h("b", {}, "Film nie może być pusty."), "Dodaj scenę z listy poniżej albo wyłącz ten temat, robiąc film od nowa.")));
}

function draw() {
  const used = new Set(Object.values(rv.reels).flat().map(s => s.scene_id)), open = rv.data.reels.filter(r => rv.reels[r.tag]);
  const unused = rv.data.scenes.filter(s => !used.has(s.id)).sort((a, b) => b.score - a.score);
  const keep = [...rv.view.querySelectorAll(".strip")].map(s => s.scrollLeft), top = window.scrollY;
  const empty = open.some(r => !rv.reels[r.tag].length);
  rv.view.classList.toggle("zones", rv.zones);
  rv.view.replaceChildren(
    h("div", {class: "row"}, h("div", {class: "cell"}, h("h1", {class: "title"}, "Przejrzyj ujęcia"),
      h("p", {class: "sub"}, "Tak będzie wyglądał każdy film, ujęcie po ujęciu. Usuń, skróć albo zamień, co chcesz. Zmiany wchodzą w życie po kliknięciu „Renderuj”."))),
    ...rv.data.reels.map(reelRow),
    h("div", {class: "gap"}),
    h("div", {class: "row"}, h("div", {class: "cell"},
      h("h2", {class: "title"}, "Niewykorzystane sceny"), h("p", {class: "sub"}, unused.length ? "Najlepsze na początku. Dodaj scenę do wybranego filmu." : "Wszystkie znalezione sceny są już w filmach."),
      h("div", {class: "tray"}, unused.map(scene => h("div", {}, thumb(scene),
        h("div", {}, h("div", {style: "font-weight:500"}, shotName(scene.type, scene.attrs)), h("div", {class: "mono muted"}, `${clock(scene.start_s)} · ${seconds(scene.end_s - scene.start_s)}`)),
        h("select", {class: "move", "aria-label": "Dodaj do filmu", style: "margin:0;min-height:32px;padding:4px 8px;font-size:13px",
                     onchange: e => { if (e.target.value) place(e.target.value, {...scene.default, framing: [...scene.default.framing]}); }},
          h("option", {value: ""}, "Dodaj do…"), open.map(r => h("option", {value: r.tag}, reelTitle(r))))))))),
    h("div", {class: "bar"},
      h("label", {class: "switch grow", style: "padding:0"}, h("input", {type: "checkbox", checked: rv.zones, onchange: e => { rv.zones = e.target.checked; rv.view.classList.toggle("zones", rv.zones); }}),
        h("span", {}, "Pokaż, co zasłoni TikTok / Instagram")),
      h("button", {class: "btn", type: "button", disabled: !rv.dirty.size || empty, onclick: guard(save)}, "Zapisz zmiany"),
      h("button", {class: "btn primary", type: "button", disabled: empty, onclick: guard(render)}, icon("film"), "Renderuj")));
  rv.view.querySelectorAll(".strip").forEach((strip, i) => { strip.scrollLeft = keep[i] || 0; });
  window.scrollTo(0, top);
}

async function save() {
  if (!rv.dirty.size) return;
  await api(`/api/jobs/${rv.id}/review`, "PUT", {reels: Object.fromEntries([...rv.dirty].map(tag => [tag, rv.reels[tag]]))});
  toast("Zapisano zmiany");
  await load();
}

async function render() {
  await save();
  await api(`/api/jobs/${rv.id}/render`, "POST");
  await refresh();
  toast("Renderuję. Postęp zobaczysz tutaj.");
  location.hash = `#filmy/${rv.id}`;
}

async function load() {
  rv.data = await api(`/api/jobs/${rv.id}/review`);
  rv.reels = {};
  rv.dirty.clear();
  for (const reel of rv.data.reels) if (reel.plan) rv.reels[reel.tag] = reel.plan.shots.map(s => ({...s, framing: [...s.framing]}));
  setContext(["Filmy", `#filmy/${rv.id}`], rv.data.job.name, "Ujęcia");
  draw();
}

export async function show(view, id) {
  rv.id = id;
  rv.view = view;
  view.replaceChildren();
  try { await load(); }
  catch (error) {
    setContext(["Filmy", "#filmy"], "Ujęcia");
    view.replaceChildren(h("div", {class: "row"}, h("div", {class: "cell empty"}, icon("eye", "icon glyph"), h("h1", {class: "title"}, "Ujęcia nie są jeszcze wybrane"),
      h("p", {}, "Ten film jest jeszcze w trakcie szukania scen albo nie udało się ich znaleźć."), h("a", {class: "btn", href: `#filmy/${id}`}, "Wróć do filmu"))));
  }
  return () => view.classList.remove("zones");
}
