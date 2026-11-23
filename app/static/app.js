// insta-roller UI: plain JS, no build step. Four screens plus the scene review.
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const STAGES = {analyze: "analiza", plan: "plan", thumbnails: "miniatury", render: "sklejanie + render", shots: "eksport ujęć"};
const STATUS = {queued: "w kolejce", review: "czeka na przegląd", done: "gotowe", error: "błąd", cancelled: "anulowane"};
const CUSTOM = "__custom__";

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  const props = {};
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else if (key === "value" || key === "checked") props[key] = value;
    else if (key === "class") el.className = value;
    else if (value === true) el.setAttribute(key, "");
    else if (value !== false && value != null) el.setAttribute(key, value);
  }
  for (const kid of kids.flat(Infinity)) if (kid != null && kid !== false) el.append(kid);
  Object.assign(el, props);            // after the options exist, so a select takes its value
  return el;
}

async function api(path, method = "GET", body) {
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
function toast(text, bad = false) {
  const el = $("#toast");
  el.textContent = text;
  el.className = bad ? "bad" : "";
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 9000 : 3000);
}
const guard = fn => async (...args) => { try { await fn(...args); } catch (error) { toast(error.message, true); } };
const options = (el, items, value) => {
  el.replaceChildren(...items.map(([v, text]) => h("option", {value: v}, text)));
  if (value != null) el.value = value;
};
const seconds = s => `${(+s).toFixed(1)} s`;
const clock = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const size = bytes => bytes > 1e9 ? `${(bytes / 1e9).toFixed(2)} GB` : `${(bytes / 1e6).toFixed(1)} MB`;
const fileUrl = (job, path, download) => `/api/jobs/${job}/files/${path.split("/").map(encodeURIComponent).join("/")}${download ? "?download=1" : ""}`;

// --- 1. Nowe zadanie -------------------------------------------------------
const form = {meta: null, listing: null};

async function screenNew() {
  form.meta = await api("/api/meta");
  const profiles = form.meta.profiles.filter(p => !p.error);
  const keep = $("#f-profile").value || "auto";
  options($("#f-profile"), [["auto", "auto – lot, gdy w folderze jest IGC, inaczej urlop"],
                            ...profiles.map(p => [p.name, p.name]), [CUSTOM, "inne – własne ustawienia"]], keep);
  options($("#f-mount"), [["", "jak w profilu"], ...form.meta.mounts.map(m => [m.name, m.name])]);
  options($("#c-base"), profiles.map(p => [p.name, p.name]));
  let last = "";
  try { last = localStorage.getItem("folder") || ""; } catch (error) { /* no storage: start at the drives */ }
  try { await browse(form.listing ? form.listing.path : last); } catch (error) { await browse(""); }
}

// The folder picker: the drives of this PC first, then any folder; a path can also be typed or pasted.
async function browse(path) {
  form.listing = await api(`/api/folders?path=${encodeURIComponent(path)}`);
  const l = form.listing;
  try { localStorage.setItem("folder", l.path); } catch (error) { /* only a convenience */ }
  $("#f-path").value = l.path;
  $("#crumbs").replaceChildren(
    h("button", {type: "button", onclick: guard(() => browse(""))}, "Ten komputer"),
    ...l.crumbs.map(c => h("button", {type: "button", onclick: guard(() => browse(c.path))}, c.name)));
  $("#dirs").replaceChildren(...l.dirs.map(d => h("li", {onclick: guard(() => browse(d.path))}, `▸ ${d.name}`)));
  if (!l.dirs.length) $("#dirs").append(h("li", {class: "muted"}, "brak podfolderów"));
  $("#folder-info").textContent = !l.path ? "Wybierz dysk i folder z nagraniami albo wklej ścieżkę powyżej."
    : `Wybrany folder: ${l.path} — nagrania: ${l.videos}, pliki IGC: ${l.igc}`
      + (l.job ? ` — ma już zadanie nr ${l.job}; ponowne dodanie przerobi tylko nowe pliki` : l.processed ? " — był już przetwarzany" : "");
  $("#f-out").placeholder = l.out || "podfolder insta-roller w folderze z nagraniami";
  syncProfile();
}

function chosenProfile() {
  const value = $("#f-profile").value, byName = name => form.meta.profiles.find(p => p.name === name && !p.error);
  if (value === CUSTOM) return byName($("#c-base").value);
  if (value === "auto") return byName(form.listing && form.listing.igc ? "flight" : "holiday");
  return byName(value);
}

function syncProfile() {
  const profile = chosenProfile(), custom = $("#f-profile").value === CUSTOM;
  $("#custom").hidden = !custom;
  $("#f-length").placeholder = profile ? `${profile.length} (z profilu)` : "z profilu";
  $("#row-mode").hidden = !!profile && profile.kind === "flight";
  const themes = (profile ? profile.themes : []).map(name => form.meta.themes.find(t => t.name === name) || {name, enabled: true});
  $("#f-themes").replaceChildren(...themes.map(t => h("label", {},
    h("input", {type: "checkbox", "data-name": t.name, "data-default": t.enabled ? "1" : "", checked: !!t.enabled}),
    `${t.title || t.name}${t.length ? ` (${t.length} s)` : ""}`)));
  if (!themes.length) $("#f-themes").append(h("span", {class: "muted"}, "tematy z profilu"));
  const mine = profile ? profile.people || [] : [];
  $("#f-people").replaceChildren(...form.meta.people.map(name => h("label", {},
    h("input", {type: "checkbox", "data-name": name, checked: mine.includes(name)}), name)));
  if (!form.meta.people.length) $("#f-people").append(h("span", {class: "muted"}, "brak zapisanych osób (people.json)"));
}

async function loadBase() {
  $("#c-yaml").value = (await api(`/api/config/profiles/${$("#c-base").value}`)).text;
  syncProfile();
}

async function compose() {
  const changes = {};
  if ($("#c-format").value) {
    const [width, height] = $("#c-format").value.split("x").map(Number);
    changes.output = {width, height};
  }
  if ($("#c-audio").value) changes.audio = $("#c-audio").value === "1";
  if ($("#c-handles").value) changes.shot_handles_s = +$("#c-handles").value;
  if ($("#f-length").value) changes.target_length_s = +$("#f-length").value;
  if ($("#f-mount").value) changes.mount = $("#f-mount").value;
  $("#c-yaml").value = (await api("/api/config/compose", "POST", {base: $("#c-base").value, changes})).text;
}

async function saveCustom() {
  const name = $("#c-name").value.trim();
  if (!name) throw new Error("Podaj nazwę nowego profilu");
  const text = $("#c-yaml").value.replace(/^name:.*$/m, `name: ${name}`);
  await api(`/api/config/profiles/${encodeURIComponent(name)}`, "PUT", {text});
  toast(`Zapisano profil ${name}`);
  await screenNew();
  $("#f-profile").value = name;
  syncProfile();
}

async function submitJob() {
  const custom = $("#f-profile").value === CUSTOM;
  const themes = [...document.querySelectorAll("#f-themes input")];
  const people = [...document.querySelectorAll("#f-people input:checked")].map(i => i.dataset.name)
    .concat($("#f-people-extra").value.split(",").map(s => s.trim()).filter(Boolean));
  if (!form.listing.path) throw new Error("Wybierz folder z nagraniami");
  const job = await api("/api/jobs", "POST", {folder: form.listing.path, options: {
    out: $("#f-out").value.trim(),
    profile: custom ? "auto" : $("#f-profile").value,
    profile_yaml: custom ? $("#c-yaml").value : "",
    mount: $("#f-mount").value,
    length: $("#f-length").value ? +$("#f-length").value : null,
    enable: themes.filter(i => i.checked && !i.dataset.default).map(i => i.dataset.name),
    disable: themes.filter(i => !i.checked && i.dataset.default).map(i => i.dataset.name),
    mode: $("#f-mode").value,
    people: [...new Set(people)],
    overlay: $("#f-overlay").value === "" ? null : $("#f-overlay").value === "1",
    review: $("#f-review").checked,
    shots: $("#f-shots").checked,
    source: $("#f-source").value,
  }});
  toast(job.existing ? (job.status === "running" ? "To zadanie właśnie się wykonuje – bez zmian" : `Zadanie nr ${job.id} wróciło do kolejki`)
                     : `Dodano zadanie nr ${job.id}`);
  location.hash = "#kolejka";
}

// --- 2. Kolejka --------------------------------------------------------------
const opened = new Set();

function statusOf(job) {
  if (job.status !== "running") return STATUS[job.status] || job.status;
  return (job.cancel ? "anulowanie… " : "") + (STAGES[job.stage] || job.stage);
}

function jobCard(job) {
  const act = (path, text, cls) => h("button", {type: "button", class: `small ${cls || ""}`,
    onclick: guard(async () => { await api(`/api/jobs/${job.id}/${path}`, "POST"); await screenQueue(); })}, text);
  const active = job.status === "queued" || job.status === "running";
  const steps = job.steps.map((s, i) => `${i < job.step_index ? "✓" : i === job.step_index && job.status === "running" ? "▶" : "·"} ${STAGES[s] || s}`).join("   ");
  return h("div", {class: "card"},
    h("div", {class: "job-head"}, h("b", {}, `#${job.id} ${job.folder}`), h("span", {class: `badge ${job.status}`}, statusOf(job))),
    h("progress", {max: 1, value: job.progress}),
    h("div", {class: "muted"}, steps),
    h("div", {class: "muted"}, `wyniki: ${job.out}`),
    job.error && h("div", {class: "error"}, job.error),
    h("details", {open: opened.has(job.id), ontoggle: e => e.target.open ? opened.add(job.id) : opened.delete(job.id)},
      h("summary", {class: "muted"}, "log i polecenia CLI"),
      h("pre", {}, job.log || "(pusty)"),
      h("pre", {}, job.commands.join("\n"))),
    h("div", {class: "row"},
      (active || job.status === "review") && act("cancel", "Anuluj", "danger"),
      (job.status === "error" || job.status === "cancelled") && act("retry", "Ponów"),
      job.status === "review" && h("a", {class: "button small", href: `#przeglad/${job.id}`}, "Przegląd scen"),
      job.status === "done" && h("a", {class: "button small", href: `#przeglad/${job.id}`}, "Popraw sceny"),
      job.step_index > 0 && h("a", {class: "button small", href: "#wyniki"}, "Wyniki")));
}

async function screenQueue() {
  const jobs = await api("/api/jobs");
  $("#jobs").replaceChildren(...jobs.map(jobCard));
  if (!jobs.length) $("#jobs").append(h("p", {class: "muted"}, "Kolejka jest pusta. Dodaj zadanie na ekranie „Nowe zadanie”."));
}

// --- 3. Wyniki ---------------------------------------------------------------
async function resultCard(job) {
  const r = await api(`/api/jobs/${job.id}/results`);
  if (!r.films.length && !r.shots.length) return null;
  const length = h("input", {type: "number", min: 5, step: 1, placeholder: "długość (s)", style: "width:9em;margin:0"});
  return h("div", {class: "card"},
    h("div", {class: "job-head"}, h("b", {}, `#${job.id} ${job.folder}`), h("span", {class: `badge ${job.status}`}, statusOf(job))),
    h("div", {class: "muted"}, `folder wyników: ${job.out}`),
    h("div", {class: "films"}, r.films.map(f => h("div", {},
      h("video", {controls: true, preload: "metadata", playsinline: true, src: fileUrl(job.id, f.name)}),
      h("div", {class: "row"}, h("span", {}, f.name), h("span", {class: "muted"}, size(f.size)),
        h("a", {class: "button small", href: fileUrl(job.id, f.name, true)}, "Pobierz"))))),
    r.shots.map(s => h("details", {},
      h("summary", {}, `Ujęcia: ${s.folder.replace("shots_", "")} (${s.files.length}) `,
        h("a", {class: "button small", href: `/api/jobs/${job.id}/zip/${encodeURIComponent(s.folder)}`}, "Pobierz paczkę ZIP")),
      h("ul", {class: "files"}, s.files.map(f => h("li", {},
        h("a", {href: fileUrl(job.id, `${s.folder}/${f.name}`), target: "_blank"}, f.name),
        h("span", {}, size(f.size), " ", h("a", {href: fileUrl(job.id, `${s.folder}/${f.name}`, true)}, "pobierz"))))))),
    r.skipped.length > 0 && h("p", {class: "muted"}, "Pominięte rolki: ", r.skipped.map(s => `${s.tag} (${s.reason})`).join("; ")),
    h("div", {class: "row"},
      h("span", {}, "Nowa wersja:"), length,
      h("button", {type: "button", onclick: guard(async () => {
        if (!length.value) throw new Error("Podaj długość nowej wersji");
        await api(`/api/jobs/${job.id}/version`, "POST", {length: +length.value});
        toast("Nowa wersja w kolejce: tylko plan i render, bez ponownej analizy");
        location.hash = "#kolejka";
      })}, "Zrób bez ponownej analizy"),
      r.report && h("a", {class: "button small", href: fileUrl(job.id, "scenes_report.txt"), target: "_blank"}, "Raport scen"),
      h("a", {class: "button small", href: `#przeglad/${job.id}`}, "Popraw sceny")));
}

async function screenResults() {
  const cards = (await Promise.all((await api("/api/jobs")).map(resultCard))).filter(Boolean);
  $("#results").replaceChildren(...cards);
  if (!cards.length) $("#results").append(h("p", {class: "muted"}, "Nie ma jeszcze gotowych filmów."));
}

// --- 4. Ustawienia -----------------------------------------------------------
async function screenSettings(keep) {
  const kind = $("#s-kind").value, meta = await api("/api/meta");
  options($("#s-name"), meta[kind].map(i => [i.name, i.error ? `${i.name} (błąd)` : i.name]), keep);
  await loadSetting();
  showAppSettings(await api("/api/settings"));
}

function showAppSettings(s) {
  $("#s-work").value = s.work;
  $("#s-data").textContent = s.data;
  $("#s-profiles").textContent = s.profiles;
}

async function saveWork() {
  showAppSettings(await api("/api/settings", "PUT", {work: $("#s-work").value.trim()}));
  toast("Zapisano folder pamięci podręcznej");
}

async function loadSetting() {
  const name = $("#s-name").value;
  $("#s-yaml").value = name ? (await api(`/api/config/${$("#s-kind").value}/${name}`)).text : "";
}

async function saveSetting() {
  const text = $("#s-yaml").value, kind = $("#s-kind").value;
  const name = (await api(`/api/config/${kind}/validate`, "POST", {text})).name;
  if (name !== $("#s-name").value && !confirm(`Zapisać jako nowy plik „${name}”?`)) return;
  await api(`/api/config/${kind}/${encodeURIComponent(name)}`, "PUT", {text});
  toast(`Zapisano ${name}`);
  await screenSettings(name);
}

// --- Przegląd scen -----------------------------------------------------------
const rv = {id: 0, data: null, reels: {}, dirty: new Set()};

async function screenReview(id) {
  rv.id = id;
  rv.data = await api(`/api/jobs/${id}/review`);
  rv.reels = {};
  rv.dirty.clear();
  for (const reel of rv.data.reels) if (reel.plan) rv.reels[reel.tag] = reel.plan.shots.map(s => ({...s}));
  $("#v-title").textContent = `#${id} ${rv.data.job.folder}`;
  drawReview();
}

const sceneOf = id => rv.data.scenes.find(s => s.id === id);
const reelLength = shots => shots.reduce((sum, s) => sum + s.duration_s / (s.speed || 1), 0);

function thumb(scene) {
  const zone = rv.data.safe_zone || {}, [w, hgt] = rv.data.aspect, pct = v => `${(v || 0) * 100}%`;
  return h("div", {class: "thumb", style: `height:${Math.round(96 * hgt / w)}px`},
    scene && scene.thumb && h("img", {loading: "lazy", alt: "", src: fileUrl(rv.id, `review/${scene.id}.jpg`)}),
    h("i", {style: `top:0;left:0;right:0;height:${pct(zone.top)}`}), h("i", {style: `bottom:0;left:0;right:0;height:${pct(zone.bottom)}`}),
    h("i", {style: `top:0;bottom:0;left:0;width:${pct(zone.left)}`}), h("i", {style: `top:0;bottom:0;right:0;width:${pct(zone.right)}`}));
}

function touch(tag) { rv.dirty.add(tag); drawReview(); }

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

function shotRow(tag, shot, index) {
  const scene = sceneOf(shot.scene_id) || {}, shots = rv.reels[tag], others = Object.keys(rv.reels).filter(t => t !== tag);
  const presets = [...new Set([...rv.data.presets, ...shot.framing])];
  return h("div", {class: "shot"}, thumb(scene),
    h("div", {},
      h("b", {}, `${index + 1}. ${shot.type}`),
      h("span", {class: "muted"}, `  ${shot.scene_id} · ${clock(shot.start_s)} · ocena ${scene.score != null ? (+scene.score).toFixed(2) : "–"}`
        + (shot.speed && shot.speed !== 1 ? ` · ×${shot.speed}` : "")),
      h("div", {class: "row"},
        h("button", {type: "button", class: "small", onclick: () => resize(shot, tag, -0.5)}, "−"),
        h("span", {class: "len"}, seconds(shot.duration_s / (shot.speed || 1))),
        h("button", {type: "button", class: "small", onclick: () => resize(shot, tag, 0.5)}, "+"),
        shot.framing.map((name, i) => h("select", {value: name, title: "preset kadru",
          onchange: e => { shot.framing[i] = e.target.value; touch(tag); }}, presets.map(p => h("option", {value: p}, p)))),
        shot.framing.length > 1 && h("button", {type: "button", class: "small", title: "usuń ostatni preset",
          onclick: () => { shot.framing.pop(); touch(tag); }}, "− kadr"),
        h("button", {type: "button", class: "small", title: "dodaj preset",
          onclick: () => { shot.framing.push(shot.framing[shot.framing.length - 1] || presets[0]); touch(tag); }}, "+ kadr")),
      h("div", {class: "row"},
        h("button", {type: "button", class: "small danger", onclick: () => { shots.splice(index, 1); touch(tag); }}, "Odrzuć"),
        others.length > 0 && h("select", {onchange: e => {
          if (!e.target.value) return;
          shots.splice(index, 1);
          rv.dirty.add(tag);
          place(e.target.value, shot);
        }}, h("option", {value: ""}, "przenieś do…"), others.map(t => h("option", {value: t}, t))))));
}

function drawReview() {
  const used = new Set(Object.values(rv.reels).flat().map(s => s.scene_id)), tags = Object.keys(rv.reels);
  $("#przeglad").classList.toggle("zone", $("#v-zone").checked);
  $("#v-reels").replaceChildren(...rv.data.reels.map(reel => {
    const shots = rv.reels[reel.tag];
    if (!shots) return h("div", {class: "card muted"}, `${reel.tag}: bez rolki – ${reel.reason}`);
    return h("div", {class: "card"},
      h("div", {class: "job-head"}, h("h3", {}, `${reel.plan.title || reel.theme} (${reel.tag})${rv.dirty.has(reel.tag) ? " •" : ""}`),
        h("span", {class: "reel-len"}, `${seconds(reelLength(shots))} / cel ${seconds(reel.plan.target_length_s)}`)),
      shots.map((shot, i) => shotRow(reel.tag, shot, i)),
      !shots.length && h("p", {class: "error"}, "Rolka nie może być pusta."));
  }));
  const unused = rv.data.scenes.filter(s => !used.has(s.id)).sort((a, b) => b.score - a.score);
  $("#v-unused").replaceChildren(...unused.map(scene => h("div", {class: "shot"}, thumb(scene),
    h("div", {},
      h("b", {}, scene.type),
      h("span", {class: "muted"}, `  ${scene.id} · ${clock(scene.start_s)} · ${seconds(scene.end_s - scene.start_s)} · ocena ${(+scene.score).toFixed(2)}`),
      h("div", {class: "row"}, h("select", {onchange: e => { if (e.target.value) place(e.target.value, {...scene.default, framing: [...scene.default.framing]}); }},
        h("option", {value: ""}, "dodaj do…"), tags.map(t => h("option", {value: t}, t))))))));
  if (!unused.length) $("#v-unused").append(h("p", {class: "muted"}, "Wszystkie sceny są w rolkach."));
}

async function saveReview() {
  if (!rv.dirty.size) return;
  await api(`/api/jobs/${rv.id}/review`, "PUT", {reels: Object.fromEntries([...rv.dirty].map(tag => [tag, rv.reels[tag]]))});
  toast("Zapisano plan rolek");
  await screenReview(rv.id);
}

async function renderReview() {
  await saveReview();
  await api(`/api/jobs/${rv.id}/render`, "POST");
  toast("Render w kolejce");
  location.hash = "#kolejka";
}

// --- routing -----------------------------------------------------------------
const SCREENS = {nowe: screenNew, kolejka: screenQueue, wyniki: screenResults, ustawienia: () => screenSettings(), przeglad: screenReview};

const route = guard(async () => {
  const [name, arg] = (location.hash.slice(1) || "kolejka").split("/");
  const screen = SCREENS[name] ? name : "kolejka";
  for (const section of document.querySelectorAll("main > section")) section.hidden = section.id !== screen;
  for (const link of document.querySelectorAll("nav a")) link.classList.toggle("on", link.hash === `#${screen}`);
  await SCREENS[screen](arg);
});

window.addEventListener("hashchange", route);
setInterval(() => { if (!document.hidden && !$("#kolejka").hidden) screenQueue().catch(() => {}); }, 3000);
$("#f-profile").addEventListener("change", guard(async () => { if ($("#f-profile").value === CUSTOM && !$("#c-yaml").value) await loadBase(); syncProfile(); }));
$("#c-base").addEventListener("change", guard(loadBase));
$("#c-compose").addEventListener("click", guard(compose));
$("#c-check").addEventListener("click", guard(async () => { await api("/api/config/profiles/validate", "POST", {text: $("#c-yaml").value}); toast("YAML jest poprawny"); }));
$("#c-save").addEventListener("click", guard(saveCustom));
$("#f-submit").addEventListener("click", guard(submitJob));
$("#f-go").addEventListener("click", guard(() => browse($("#f-path").value)));
$("#f-path").addEventListener("keydown", e => { if (e.key === "Enter") $("#f-go").click(); });
$("#s-work-save").addEventListener("click", guard(saveWork));
$("#r-refresh").addEventListener("click", guard(screenResults));
$("#s-kind").addEventListener("change", guard(() => screenSettings()));
$("#s-name").addEventListener("change", guard(loadSetting));
$("#s-check").addEventListener("click", guard(async () => { await api(`/api/config/${$("#s-kind").value}/validate`, "POST", {text: $("#s-yaml").value}); toast("YAML jest poprawny"); }));
$("#s-save").addEventListener("click", guard(saveSetting));
$("#v-zone").addEventListener("change", drawReview);
$("#v-save").addEventListener("click", guard(saveReview));
$("#v-render").addEventListener("click", guard(renderReview));
route();
