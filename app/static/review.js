// Ujęcia: the third step of making reels. Each reel as a strip of shots in order: look at them, remove,
// shorten, lengthen, change the camera view, move between reels, add unused moments; then make the reels.
import {$, h, api, icon, guard, toast, seconds, clock, comma, shotName, presetName, fileUrl, stateText, problemText, skipText, lastSaid, duration, flowSteps, THEMES} from "./ui.js";
import {live, watch, refresh, setContext} from "./live.js";

const rv = {id: 0, data: null, reels: {}, dirty: new Set(), zones: false, view: null,
            stills: new Map(), asked: [], clips: new Map(), active: new WeakMap(), pick: null, quality: null, status: null};
// Seconds of work for one second of reel, by quality: orders of magnitude, not promises.
const QUALITIES = [["best", "Najlepsza", 15], ["good", "Dobra", 5], ["fast", "Szybki podgląd", 1]];
const about = s => s < 90 ? "około minuty" : s < 3600 ? `około ${Math.round(s / 60)} min` : `około ${(s / 3600).toFixed(1).replace(".", ",")} godz.`;
const aiWhy = shot => { const text = (shot.attrs || {}).ai || ""; return /^local:/.test(text) ? "" : text.replace(/^[^:]*:\s*/, "").trim(); };
const REASONS = {"theme is switched off": "ten temat jest wyłączony", "no usable material": "za mało pasujących scen"};

const sceneOf = id => rv.data.scenes.find(s => s.id === id);
const filmLength = shots => shots.reduce((sum, s) => sum + s.duration_s / (s.speed || 1), 0);
const reelTitle = reel => (reel.plan && reel.plan.title) || (THEMES[reel.theme] || [])[0] || reel.theme;
function touch(tag) { rv.dirty.add(tag); draw(); }

// --- previews: what a view shows at the moment of the shot, made on demand from the proxy ---------
const moment = shot => Math.round(shot.start_s + shot.duration_s / 2);
const stillKey = (shot, preset) => [shot.scene_id, moment(shot), preset, JSON.stringify(((shot.attrs || {}).views || {})[preset] || null),
                                    JSON.stringify(Object.entries(shot.attrs || {}).filter(([k]) => /^(yaw|pitch)_/.test(k)))].join("|");
function still(shot, preset) {                   // the picture's address, or null while it is being made
  const key = stillKey(shot, preset);
  if (!rv.stills.has(key)) {
    rv.stills.set(key, null);
    rv.asked.push({key, ask: {shot: {scene_id: shot.scene_id, recording: shot.recording || "", attrs: shot.attrs || {}}, at: moment(shot), preset}});
  }
  return rv.stills.get(key);
}
let fetching = false;
async function fetchStills() {                   // one request for everything the screen asked for
  if (fetching || !rv.asked.length) return;
  fetching = true;
  const batch = rv.asked.splice(0, 40), id = rv.id;
  try {
    const answer = await api(`/api/jobs/${id}/preview`, "POST", {stills: batch.map(b => b.ask)});
    batch.forEach((b, i) => rv.stills.set(b.key, answer.stills[i] ? fileUrl(id, `review/${answer.stills[i]}`) : false));
  } catch { batch.forEach(b => rv.stills.set(b.key, false)); }
  fetching = false;
  if (rv.id === id && rv.view.isConnected) { draw(); drawPicker(); }
}
// A shot keeps one projection: the camera can turn and zoom during it, but not change its lens.
const KINDS = {flat: "zwykły widok", planet: "mała planeta", fisheye: "rybie oko"};
const kindOf = (shot, name) => (((shot.attrs || {}).views || {})[name] || {}).projection || (rv.data.projections || {})[name] || "flat";
const mixed = shot => new Set(shot.framing.map(name => kindOf(shot, name))).size > 1;
const clipKey = shot => JSON.stringify([shot.scene_id, shot.start_s, shot.duration_s, shot.framing, shot.speed || 1, (shot.attrs || {}).views || null]);
async function playClip(shot) {
  const key = clipKey(shot), id = rv.id;
  rv.clips.set(key, null);
  draw();
  try {
    const answer = await api(`/api/jobs/${id}/preview`, "POST", {clip: shot});
    rv.clips.set(key, answer.clip ? fileUrl(id, `review/${answer.clip}`) : false);
    if (!answer.clip) toast("Podgląd się nie udał. Spróbuj ponownie albo zmień kadr.");
  } catch { rv.clips.set(key, false); }
  if (rv.id === id && rv.view.isConnected) draw();
}

function thumb(scene, picture, clip) {           // picture: a still's address, null = on its way, undefined = the scene's own
  const zone = rv.data.safe_zone || {}, [w, hgt] = rv.data.aspect, pct = v => `${(v || 0) * 100}%`;
  const fallback = scene && scene.thumb && fileUrl(rv.id, `review/${scene.id}.jpg`);
  return h("div", {class: `thumb${picture === null ? " loading" : ""}`, style: `aspect-ratio:${w}/${hgt}`},
    clip ? h("video", {src: clip, autoplay: true, loop: true, muted: true, playsinline: true, controls: true})
      : (picture || fallback) && h("img", {loading: "lazy", alt: "", src: picture || fallback}),
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
  const film = shot.duration_s / (shot.speed || 1);
  const active = Math.min(rv.active.get(shot) || 0, shot.framing.length - 1);
  const clip = rv.clips.get(clipKey(shot));
  return h("div", {class: "shot"},
    h("div", {class: "spread", style: "flex-wrap:nowrap"},
      h("span", {class: "no"}, `${index + 1} · ${clock(shot.start_s)}${shot.speed && shot.speed !== 1 ? ` · ×${shot.speed}` : ""}`),
      h("button", {class: "btn small quiet danger", type: "button", onclick: () => { shots.splice(index, 1); touch(tag); }}, "Usuń")),
    thumb(scene, still(shot, shot.framing[active]), clip),
    mixed(shot) && h("p", {class: "problem", style: "margin:0;font-size:12px"}, "Kadry tego ujęcia mają różne projekcje. Zmień jeden z nich, inaczej ujęcia nie da się zrobić."),
    h("button", {class: "btn small quiet", type: "button", disabled: clip === null || mixed(shot), onclick: () => { if (clip) { rv.clips.delete(clipKey(shot)); draw(); } else playClip(shot); }},
      clip === null ? "Robię podgląd…" : clip ? "Pokaż klatkę" : "▶ Podgląd ruchu"),
    h("div", {class: "what"}, shotName(shot.type, {...scene.attrs, ...shot.attrs})),
    aiWhy(shot) && h("p", {class: "why", title: "Dlaczego AI tak ustawiło kamerę w tym ujęciu"}, h("b", {}, "AI: "), aiWhy(shot)),
    h("div", {class: "len"},
      h("button", {class: "btn", type: "button", "aria-label": "Krócej o pół sekundy", title: "Krócej", onclick: () => resize(shot, tag, -0.5)}, "−"),
      h("b", {}, `${comma(film)} s`),
      h("button", {class: "btn", type: "button", "aria-label": "Dłużej o pół sekundy", title: "Dłużej", onclick: () => resize(shot, tag, 0.5)}, "+")),
    h("div", {}, h("div", {class: "muted", style: "font-size:12px;margin-bottom:4px"}, shot.framing.length > 1 ? "Kadr: kamera przechodzi kolejno" : "Kadr"),
      h("div", {class: "chips"},
        shot.framing.map((name, i) => h("span", {class: `chip kadr${i === active ? " on" : ""}`},
          h("button", {class: "see", type: "button", "aria-label": `Pokaż kadr ${i + 1}: ${presetName(name)}`, title: "Pokaż ten kadr na miniaturze",
                       onclick: () => { rv.active.set(shot, i); rv.clips.delete(clipKey(shot)); draw(); }},
            still(shot, name) ? h("img", {alt: "", src: still(shot, name)}) : h("i", {})),
          h("button", {class: "name", type: "button", title: "Zmień kadr: wybierz z podglądów", onclick: () => openPicker(tag, shot, i)}, presetName(name)),
          shot.framing.length > 1 && h("button", {class: "x", type: "button", "aria-label": "Usuń ten kadr", onclick: () => { shot.framing.splice(i, 1); touch(tag); }}, "×"))),
        shot.framing.length < 3 && h("button", {class: "chip add", type: "button", onclick: () => { shot.framing.push(shot.framing[shot.framing.length - 1]); rv.dirty.add(tag); openPicker(tag, shot, shot.framing.length - 1); draw(); }}, "+ kadr"))),
    others.length > 0 && h("select", {class: "move", "aria-label": "Przenieś do innej rolki", onchange: e => {
      if (!e.target.value) return;
      shots.splice(index, 1);
      rv.dirty.add(tag);
      place(e.target.value, shot);
    }}, h("option", {value: ""}, "Przenieś do innej rolki…"), others.map(r => h("option", {value: r.tag}, reelTitle(r)))));
}

// --- choosing a view by its picture -----------------------------------------------------------------
function openPicker(tag, shot, index) {
  rv.pick = {tag, shot, index};
  let dialog = $("#kadr-picker");
  if (!dialog) {
    dialog = h("dialog", {id: "kadr-picker", class: "picker"});
    dialog.addEventListener("close", () => { rv.pick = null; });
    dialog.addEventListener("click", e => { if (e.target === dialog) dialog.close(); });
    document.body.append(dialog);
  }
  drawPicker();
  if (!dialog.open) dialog.showModal();
}
function drawPicker() {
  const dialog = $("#kadr-picker");
  if (!dialog || !rv.pick) return;
  const {tag, shot, index} = rv.pick, [w, hgt] = rv.data.aspect;
  const names = [...new Set([...Object.keys((shot.attrs || {}).views || {}), ...rv.data.presets])];
  const others = shot.framing.filter((_, i) => i !== index).map(name => kindOf(shot, name)), need = others[0];
  const fits = name => !need || kindOf(shot, name) === need;
  dialog.replaceChildren(
    h("div", {class: "spread"}, h("h2", {class: "title"}, "Wybierz kadr"),
      h("button", {class: "btn small quiet", type: "button", onclick: () => dialog.close()}, "Zamknij")),
    h("p", {class: "muted", style: "font-size:13px;margin:4px 0 14px"},
      `Tak wygląda ta chwila (${clock(moment(shot))}) w każdym z kadrów. Kadry z ruchem pokazują swój początek; cały ruch zobaczysz w „Podglądzie ruchu”.`
      + (need ? ` To ujęcie ma już kadr typu „${KINDS[need]}”, więc pozostałe muszą być tego samego typu: reszta jest wyszarzona.` : "")),
    h("div", {class: "kadry"}, names.map(name => {
      const picture = still(shot, name);
      return h("button", {type: "button", class: name === shot.framing[index] ? "on" : "", disabled: !fits(name),
                          title: fits(name) ? "" : `Inna projekcja (${KINDS[kindOf(shot, name)]}) niż pozostałe kadry tego ujęcia`, onclick: () => { shot.framing[index] = name; rv.active.set(shot, index); dialog.close(); touch(tag); }},
        h("span", {class: `thumb${picture === null ? " loading" : ""}`, style: `aspect-ratio:${w}/${hgt}`}, picture && h("img", {alt: "", src: picture})),
        h("span", {}, presetName(name)));
    })));
  fetchStills();
}

// --- the whole reel as a rough cut: the previews of its shots, one after another ----------------------
async function playReel(tag, title) {
  const shots = rv.reels[tag].filter(s => !mixed(s)), id = rv.id;
  const note = h("p", {class: "muted", style: "font-size:13px;margin:0 0 12px"}, "Przygotowuję podgląd…");
  const video = h("video", {controls: true, playsinline: true, muted: true, style: "max-height:70vh;max-width:100%;display:block;margin:0 auto;background:#000;border-radius:6px"});
  const dialog = h("dialog", {class: "picker", style: "width:min(560px,calc(100vw - 32px))"},
    h("div", {class: "spread"}, h("h2", {class: "title"}, `Podgląd: ${title}`),
      h("button", {class: "btn small quiet", type: "button", onclick: () => dialog.close()}, "Zamknij")),
    note, video);
  let open = true;
  dialog.addEventListener("close", () => { open = false; video.pause(); dialog.remove(); });
  dialog.addEventListener("click", e => { if (e.target === dialog) dialog.close(); });
  document.body.append(dialog);
  dialog.showModal();
  const clips = [];
  for (const [i, shot] of shots.entries()) {            // made one by one; the ones made before are remembered
    if (!open) return;
    note.textContent = `Przygotowuję podgląd: ujęcie ${i + 1} z ${shots.length}. To mała rozdzielczość, tylko żeby zobaczyć ruch i kolejność.`;
    const key = clipKey(shot);
    let url = rv.clips.get(key);
    if (!url) {
      try { const answer = await api(`/api/jobs/${id}/preview`, "POST", {clip: shot}); url = answer.clip ? fileUrl(id, `review/${answer.clip}`) : false; }
      catch { url = false; }
    }
    if (url) clips.push(url);
  }
  if (!open) return;
  if (!clips.length) { note.textContent = "Nie udało się przygotować podglądu tej rolki."; return; }
  let at = 0;
  const next = () => { note.textContent = `Ujęcie ${at + 1} z ${clips.length}. Mała rozdzielczość, bez dźwięku: gotowa rolka będzie ostra.`; video.src = clips[at]; video.play().catch(() => {}); };
  video.addEventListener("ended", () => { at = (at + 1) % clips.length; next(); });
  next();
}

const resetReel = (reel) => guard(async () => {
  if (!confirm(`Przywrócić automatyczny wybór ujęć w rolce „${reelTitle(reel)}”? Twoje zmiany w tej rolce przepadną.`)) return;
  if (reel.plan && reel.plan.edited) {                  // saved edits: the reel is planned again
    await api(`/api/jobs/${rv.id}/review/reset`, "POST", {tags: [reel.tag]});
    await refresh();
    toast("Układam tę rolkę od nowa.");
    return show(rv.view, rv.id);
  }
  rv.reels[reel.tag] = reel.plan.shots.map(sh => ({...sh, framing: [...sh.framing]}));
  rv.dirty.delete(reel.tag);
  draw();
});

function reelRow(reel) {
  const shots = rv.reels[reel.tag];
  if (!shots) return h("div", {class: "row"}, h("div", {class: "cell tight muted"}, `Rolka „${reelTitle(reel)}” nie powstanie: ${REASONS[reel.reason] || skipText(reel.reason)}`));
  const now = filmLength(shots), target = reel.plan.target_length_s, over = now > target * 1.15;
  return h("div", {class: "row"}, h("div", {class: "cell flush"},
    h("div", {style: "padding:24px 32px 16px"},
      h("div", {class: "spread"},
        h("h2", {class: "title"}, reelTitle(reel), rv.dirty.has(reel.tag) && h("span", {class: "live"}, "[ ZMIENIONE ]")),
        h("div", {class: "inline"},
          (rv.dirty.has(reel.tag) || reel.plan.edited) && h("button", {class: "btn small quiet", type: "button", onclick: resetReel(reel)}, "Przywróć automatyczne"),
          h("button", {class: "btn small", type: "button", disabled: !shots.length, onclick: () => playReel(reel.tag, reelTitle(reel))}, icon("play", "icon solid"), "Podgląd całej rolki"),
          h("div", {class: "mono"}, h("b", {style: "font-weight:500"}, seconds(now)), h("span", {class: "muted"}, ` z ${seconds(target)}`)))),
      h("div", {class: `lenbar${over ? " over" : ""}`}, h("i", {style: `width:${Math.min(now / target * 100, 100)}%`})),
      over && h("p", {class: "muted", style: "font-size:13px;margin-top:8px"}, "Rolka jest dłuższa, niż planowano. To nie błąd, ale możesz skrócić ujęcia.")),
    shots.length ? h("div", {class: "strip", tabindex: 0, "aria-label": `Ujęcia filmu ${reelTitle(reel)}, po kolei`}, shots.map((shot, i) => shotCard(reel.tag, shot, i)))
      : h("p", {class: "problem", style: "margin:0 32px 24px"}, h("b", {}, "Rolka nie może być pusta."), "Dodaj moment z listy poniżej albo kliknij „Przywróć automatyczne”.")));
}

function draw() {
  const used = new Set(Object.values(rv.reels).flat().map(s => s.scene_id)), open = rv.data.reels.filter(r => rv.reels[r.tag]);
  const unused = rv.data.scenes.filter(s => !used.has(s.id)).sort((a, b) => b.score - a.score);
  const keep = [...rv.view.querySelectorAll(".strip")].map(s => s.scrollLeft), top = window.scrollY;
  const empty = open.some(r => !rv.reels[r.tag].length) || open.some(r => rv.reels[r.tag].some(mixed));
  rv.view.classList.toggle("zones", rv.zones);
  rv.view.replaceChildren(
    flowSteps(3),
    h("div", {class: "row"}, h("div", {class: "cell"}, h("h1", {class: "title"}, "Sprawdź ujęcia"),
      h("p", {class: "sub"}, "Tak będzie wyglądała każda rolka, ujęcie po ujęciu. Jeśli wszystko pasuje, kliknij „Zrób rolki” na dole. Jeśli nie: usuń, skróć albo zmień kadr. Nic nie jest jeszcze zrobione na czysto."))),
    ...rv.data.reels.map(reelRow),
    h("div", {class: "gap"}),
    h("div", {class: "row"}, h("div", {class: "cell"},
      h("h2", {class: "title"}, "Niewykorzystane momenty"), h("p", {class: "sub"}, unused.length ? `Jeszcze ${unused.length} momentów z nagrania, najlepsze na początku. Dodaj dowolny do wybranej rolki.` : "Wszystkie znalezione momenty są już w rolkach."),
      h("div", {class: "tray"}, unused.map(scene => h("div", {}, thumb(scene),
        h("div", {}, h("div", {style: "font-weight:500"}, shotName(scene.type, scene.attrs)), h("div", {class: "mono muted"}, `${clock(scene.start_s)} · ${seconds(scene.end_s - scene.start_s)}`)),
        h("select", {class: "move", "aria-label": "Dodaj do rolki", style: "margin:0;min-height:32px;padding:4px 8px;font-size:13px",
                     onchange: e => { if (e.target.value) place(e.target.value, {...scene.default, framing: [...scene.default.framing]}); }},
          h("option", {value: ""}, "Dodaj do…"), open.map(r => h("option", {value: r.tag}, reelTitle(r))))))))),
    finalBar(open, empty));
  rv.view.querySelectorAll(".strip").forEach((strip, i) => { strip.scrollLeft = keep[i] || 0; });
  window.scrollTo(0, top);
  fetchStills();
}

// The one decision at the end: how good, and go. Saving happens by itself.
function finalBar(open, empty) {
  const can = {best: !!(rv.status && rv.status.stitcher), good: true, fast: true};
  const wish = rv.quality || (rv.data.job.quality !== "auto" ? rv.data.job.quality : null);
  const quality = wish && can[wish] ? wish : can.best ? "best" : "good";
  const film = open.reduce((sum, r) => sum + filmLength(rv.reels[r.tag]), 0), factor = QUALITIES.find(q => q[0] === quality)[2];
  const n = open.length;
  return h("div", {class: "bar final"},
    h("label", {class: "switch", style: "padding:0"}, h("input", {type: "checkbox", checked: rv.zones, onchange: e => { rv.zones = e.target.checked; rv.view.classList.toggle("zones", rv.zones); }}),
      h("span", {}, "Pokaż, co zasłoni TikTok / Instagram")),
    h("span", {class: "grow"}),
    h("label", {class: "inline", style: "gap:8px;font-size:14px"}, "Jakość",
      h("select", {style: "min-height:40px", onchange: e => { rv.quality = e.target.value; draw(); }}, QUALITIES.map(([value, title]) =>
        h("option", {value, selected: value === quality, disabled: !can[value]}, can[value] ? title : `${title} (niedostępna)`)))),
    h("span", {class: "muted", style: "font-size:13px"}, empty ? "Najpierw popraw rolki zaznaczone na czerwono." : `${n} ${n === 1 ? "rolka" : n < 5 ? "rolki" : "rolek"}, ${about(film * factor + 60)} pracy`),
    rv.dirty.size > 0 && h("button", {class: "btn", type: "button", disabled: empty, onclick: guard(async () => { await save(); toast("Zapisane. Rolki zrobisz, kiedy zechcesz."); })}, "Zapisz i wróć później"),
    h("button", {class: "btn primary big", type: "button", disabled: empty, onclick: guard(() => render(quality))}, icon("film"), "Zrób rolki"));
}

async function save() {
  if (!rv.dirty.size) return;
  await api(`/api/jobs/${rv.id}/review`, "PUT", {reels: Object.fromEntries([...rv.dirty].map(tag => [tag, rv.reels[tag]]))});
  await load();
}

async function render(quality) {
  await save();
  await api(`/api/jobs/${rv.id}/render`, "POST", {quality});
  await refresh();
  toast("Robię rolki. Postęp zobaczysz tutaj.");
  location.hash = `#filmy/${rv.id}`;
}

async function load() {
  rv.data = await api(`/api/jobs/${rv.id}/review`);
  rv.reels = {};
  rv.dirty.clear();
  for (const reel of rv.data.reels) if (reel.plan) rv.reels[reel.tag] = reel.plan.shots.map(s => ({...s, framing: [...s.framing]}));
  setContext(["Moje nagrania", `#filmy/${rv.id}`], rv.data.job.label || rv.data.job.name, "Ujęcia");
  draw();
}

// While the moments are still being looked for: say so, and open the shots by itself when they are there.
const waitText = job => [h("i", {"aria-hidden": "true"}), h("span", {}, job.status === "queued" ? "Czeka w kolejce" : lastSaid(job) || stateText(job)),
                         h("span", {class: "muted"}, ` · ${stateText(job)} · trwa ${duration(job.elapsed_s)}`)];
function waiting(job) {
  const [what, hint] = job.status === "error" ? problemText(job) : [];
  setContext(["Moje nagrania", `#filmy/${job.id}`], job.label || job.name, "Ujęcia");
  rv.view.replaceChildren(flowSteps(3),
    h("div", {class: "row"}, h("div", {class: "cell empty"}, icon(what ? "alert" : "eye", "icon glyph"),
      h("h1", {class: "title"}, what || (job.status === "cancelled" ? "Zatrzymane" : "Szukam najlepszych momentów")),
      h("p", {}, what ? hint : job.status === "cancelled" ? "Szukanie momentów zostało zatrzymane." : "Gdy skończę, pokażę tutaj wybrane ujęcia. Możesz w tym czasie robić coś innego."),
      !what && job.status !== "cancelled" && h("div", {class: "working", role: "progressbar", "aria-label": "Trwa praca"}),
      !what && job.status !== "cancelled" && h("p", {class: "alive", style: "justify-content:center", id: "rv-wait"}, waitText(job)),
      what || job.status === "cancelled" ? h("div", {class: "inline", style: "justify-content:center"},
          h("button", {class: "btn primary", type: "button", onclick: guard(async () => { await api(`/api/jobs/${job.id}/retry`, "POST"); await refresh(); show(rv.view, job.id); })}, "Spróbuj ponownie"),
          h("a", {class: "btn", href: `#filmy/${job.id}`}, "Zobacz szczegóły"))
        : h("div", {class: "inline", style: "justify-content:center"},
          h("button", {class: "btn", type: "button", onclick: guard(async () => { await api(`/api/jobs/${job.id}/cancel`, "POST"); await refresh(); })}, "Zatrzymaj")))));
}

export async function show(view, id) {
  rv.id = id;
  rv.view = view;
  rv.stills.clear(); rv.clips.clear(); rv.asked.length = 0;
  rv.quality = null;
  view.replaceChildren();
  api("/api/status").then(status => { rv.status = status; if (rv.id === id && rv.data && view.isConnected) draw(); }).catch(() => {});
  let stop = null, shown = false;
  const open = async () => {
    try { await load(); shown = true; }
    catch (error) {
      shown = false;
      setContext(["Moje nagrania", "#filmy"], "Ujęcia");
      view.replaceChildren(flowSteps(3), h("div", {class: "row"}, h("div", {class: "cell empty"}, icon("eye", "icon glyph"), h("h1", {class: "title"}, "Ujęcia nie są jeszcze wybrane"),
        h("p", {}, "Dla tych nagrań nie ma jeszcze znalezionych momentów."), h("a", {class: "btn", href: `#filmy/${id}`}, "Wróć do nagrania"))));
    }
  };
  const job = live.jobs.find(j => j.id === id);
  const busy = j => j && (j.status === "running" || j.status === "queued");
  if (busy(job) || (job && (job.status === "error" || job.status === "cancelled"))) waiting(job); else await open();
  let last = job ? job.status : "";
  stop = watch(jobs => {                         // the shots open by themselves when the search is over
    const now = jobs.find(j => j.id === id);
    if (!now || rv.id !== id || !view.isConnected) return;
    if (!shown && busy(now)) { const text = view.querySelector("#rv-wait"); if (text) text.replaceChildren(...waitText(now)); }
    if (now.status !== last) {
      last = now.status;
      if (busy(now) || now.status === "error" || now.status === "cancelled") { shown = false; waiting(now); }
      else open();
    }
  });
  return () => { if (stop) stop(); view.classList.remove("zones"); };
}
