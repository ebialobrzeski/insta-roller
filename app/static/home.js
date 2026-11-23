// Przegląd: what to do, the current film as a picture, the state of the PC, the finished films.
import {h, api, icon, size, date, duration, plural, shotName, stateText, fileUrl, clock} from "./ui.js";
import {live, watch, setContext, play} from "./live.js";

const STARTS = [
  ["lot", "glider", "Rolki z lotu", "Krótkie pionowe filmy 9:16 z szybowca: start, komin, przelot."],
  ["urlop", "beach", "Film z urlopu", "Poziomy film 16:9 z kija do selfie, dzień po dniu."],
  ["ujecia", "cuts", "Same ujęcia", "Najlepsze momenty jako osobne pliki do montażu w telefonie."],
  ["wlasne", "sliders", "Własne ustawienia", "Inny format, dźwięk albo długość filmu."],
];

export const filmOf = f => ({...f, url: fileUrl(f.job, f.name)});

// --- the barogram: altitude over the flight, with the chosen shots on it -------------------
export function barogram(box, data) {
  const points = data.points;
  if (points.length < 2) { box.replaceChildren(h("p", {class: "muted"}, "Brak danych o wysokości dla tego lotu.")); return; }
  const W = Math.max(box.clientWidth, 280), H = Math.round(Math.min(Math.max(W * .36, 170), 280)), pad = {t: 14, b: 26};
  const t0 = points[0][0], t1 = points[points.length - 1][0];
  const alts = points.map(p => p[1]), lo = Math.min(...alts), hi = Math.max(...alts), span = Math.max(hi - lo, 1);
  const x = t => (t - t0) / (t1 - t0) * W, y = a => pad.t + (1 - (a - lo) / span) * (H - pad.t - pad.b);
  const altAt = t => {
    let i = points.findIndex(p => p[0] >= t);
    if (i <= 0) return points[i === 0 ? 0 : points.length - 1][1];
    const [ta, a] = points[i - 1], [tb, b] = points[i];
    return a + (b - a) * (t - ta) / (tb - ta || 1);
  };
  const line = points.map((p, i) => `${i ? "L" : "M"}${x(p[0]).toFixed(1)} ${y(p[1]).toFixed(1)}`).join("");
  const base = H - pad.b, id = `fill${Math.random().toString(36).slice(2, 8)}`;
  const seen = new Set(), shots = data.shots.filter(s => !seen.has(s.start_s) && seen.add(s.start_s) && s.start_s >= t0 && s.start_s <= t1);
  const hours = (t1 - t0) > 5400, label = t => hours ? `${Math.floor(t / 3600)}:${String(Math.floor(t % 3600 / 60)).padStart(2, "0")}` : clock(t);
  box.replaceChildren(h("svg", {viewBox: `0 0 ${W} ${H}`, role: "img",
      "aria-label": `Wysokość w czasie lotu: od ${Math.round(lo)} do ${Math.round(hi)} m. Zaznaczone ujęcia: ${shots.length}.`},
    h("defs", {}, h("linearGradient", {id, x1: 0, y1: 0, x2: 0, y2: 1},
      h("stop", {offset: "0", "stop-color": "var(--accent)", "stop-opacity": ".30"}), h("stop", {offset: "1", "stop-color": "var(--accent)", "stop-opacity": "0"}))),
    shots.map(s => h("rect", {class: "span", x: x(s.start_s), y: pad.t, width: Math.max(x(s.start_s + s.duration_s) - x(s.start_s), 2), height: base - pad.t})),
    h("path", {d: `${line}L${W} ${base}L0 ${base}Z`, fill: `url(#${id})`}),
    h("path", {class: "line", d: line}),
    h("line", {class: "base", x1: 0, x2: W, y1: base, y2: base}),
    shots.map(s => { const t = s.start_s + s.duration_s / 2; return h("circle", {class: "shot", cx: x(t), cy: y(altAt(t)), r: 4},
      h("title", {}, `${shotName(s.type)} · ${label(t - t0)}`)); }),
    h("text", {x: 2, y: H - 8}, label(0)),
    h("text", {x: W / 2, y: H - 8, "text-anchor": "middle"}, label((t1 - t0) / 2)),
    h("text", {x: W - 2, y: H - 8, "text-anchor": "end"}, label(t1 - t0)),
    h("text", {x: W - 2, y: Math.max(y(hi) - 4, 10), "text-anchor": "end"}, `${Math.round(hi)} m`)));
}

function dayStrip(data) {
  const longest = Math.max(...data.days.map(d => d.duration_s), 1);
  return h("div", {class: "days", role: "img", "aria-label": `Nagrania z ${plural(data.days.length, "dnia", "dni", "dni")}`},
    data.days.map(d => h("div", {style: `height:${Math.max(d.duration_s / longest * 100, 5)}%`, title: `${d.label}: ${plural(d.clips, "nagranie", "nagrania", "nagrań")}, ${duration(d.duration_s)}`},
      h("b", {}, d.clips), h("span", {}, d.label.slice(0, 5)))));
}

function current(job, data, films) {
  const all = films.filter(f => f.job === job.id), mine = all.length, now = job.status === "done" ? all.filter(f => f.current) : [];
  const busy = job.status === "running" || job.status === "queued";
  const flight = data && data.kind === "flight";
  const big = data ? (flight ? new Set(data.shots.map(s => s.start_s)).size : data.shots) : mine;
  const box = h("div", {class: "chart"});
  const cell = h("div", {class: "cell"},
    h("div", {class: "head"},
      h("div", {style: "min-width:0"},
        h("h2", {class: "title"}, job.name, busy && h("span", {class: "live"}, "[ W TOKU ]")),
        h("p", {class: "sub", id: "home-state"}, subtitle(job, data))),
      h("div", {style: "text-align:right"}, h("div", {class: "big"}, big), h("div", {class: "muted", style: "font-size:13px;white-space:nowrap"}, data ? "wybranych ujęć" : "filmów"))),
    !data ? h("p", {class: "muted mt2"}, busy ? "Wykres pojawi się, gdy skończy się szukanie scen." : "Ten film nie ma jeszcze znalezionych scen.")
      : flight ? box : dayStrip(data),
    flight && h("p", {class: "muted", style: "font-size:13px;margin-top:10px"}, "Wysokość w czasie lotu. Kółka i pasy to miejsca, z których pochodzą ujęcia."),
    now.length ? h("div", {class: "inline mt2"}, now.slice(0, 6).map(f => h("button", {class: "btn small", type: "button", onclick: () => play(filmOf(f))},
      icon("play", "icon solid"), f.title, h("span", {class: "mono muted"}, f.length_s ? `${f.length_s} s` : "")))) : null,
    h("div", {class: "inline mt2"},
      h("a", {class: "btn", href: `#filmy/${job.id}`}, icon("film"), mine ? "Wszystkie filmy z tego folderu" : "Zobacz postęp"),
      job.status === "review" && h("a", {class: "btn primary", href: `#filmy/${job.id}/ujecia`}, "Przejrzyj ujęcia")));
  return [cell, flight ? () => barogram(box, data) : null];
}

function subtitle(job, data) {
  const kind = data && data.kind === "holiday" ? `Urlop, ${plural(data.days.length, "dzień", "dni", "dni")}` : data ? `Lot, ${duration(data.points.length ? data.points[data.points.length - 1][0] - data.points[0][0] : 0)}` : "";
  return [kind, stateText(job), date(job.queued)].filter(Boolean).join(" · ");
}

function status(s) {
  const row = (name, value, ok) => [h("dt", {}, name), h("dd", {class: ok ? "ok" : ok === false ? "muted" : ""}, value)];
  return h("div", {class: "cell"},
    h("h2", {class: "title"}, "Stan"), h("p", {class: "sub"}, "Czym ten komputer robi filmy"),
    h("dl", {class: "kv"},
      row("Najlepsza jakość", s.stitcher ? "dostępna" : "niedostępna", s.stitcher),
      row("Karta graficzna", s.gpu || "nie wykryto", !!s.gpu),
      row("Wolne miejsce na dysku", s.cache.free_bytes == null ? "nieznane" : size(s.cache.free_bytes)),
      row("Osoby do rozpoznania", s.people.present ? plural(s.people.count, "osoba", "osoby", "osób") : "brak listy", s.people.present)),
    !s.stitcher && h("p", {class: "muted mt", style: "font-size:13px"}, "Bez programu do sklejania Media SDK filmy powstają w jakości „Dobra”: na łączeniu obiektywów bywa widoczny szew."));
}

function latest(films) {
  return h("div", {class: "cell flush"},
    h("div", {style: "padding:28px 32px 16px"}, h("h2", {class: "title"}, "Ostatnie filmy"), h("p", {class: "sub"}, films.length ? "Kliknij, żeby obejrzeć" : "Tu pojawią się gotowe filmy")),
    h("ul", {class: "list"}, films.slice(0, 4).map(f => h("li", {},
      h("button", {class: "round", type: "button", "aria-label": `Odtwórz: ${f.title}`, onclick: () => play(filmOf(f))}, icon("play")),
      h("div", {class: "grow"}, h("div", {class: "name"}, f.title), h("div", {class: "mono muted name"}, `${f.job_name} · ${f.length_s ? `${f.length_s} s` : ""}`)),
      h("a", {class: "btn small quiet", href: fileUrl(f.job, f.name, true), "aria-label": `Pobierz: ${f.title}`, title: "Pobierz"}, icon("down"))))));
}

function grid(films) {
  return films.slice(0, 12).map(f => h("button", {class: "cell tight", type: "button", onclick: () => play(filmOf(f)), "aria-label": `Odtwórz: ${f.title}, ${f.job_name}`},
    h("div", {class: "spread", style: "flex-wrap:nowrap"},
      h("div", {style: "min-width:0"}, h("div", {class: "h3"}, f.title), h("div", {class: "mono muted", style: "overflow:hidden;text-overflow:ellipsis;white-space:nowrap"}, f.job_name)),
      h("span", {class: "round", "aria-hidden": "true"}, icon("play"))),
    h("div", {class: "tags", style: "margin-top:12px"},
      f.length_s && h("span", {class: "tag"}, `${f.length_s} s`), h("span", {class: "tag plain"}, date(f.mtime)), h("span", {class: "tag plain"}, size(f.size)))));
}

export async function show(view) {
  setContext("Przegląd");
  const [every, state] = await Promise.all([api("/api/films"), api("/api/status")]);
  const films = every.some(f => f.current) ? every.filter(f => f.current) : every;      // not the earlier versions of other lengths
  const jobs = live.jobs, job = jobs.find(j => j.status === "running") || jobs[0];
  let data = null;
  if (job) { try { data = await api(`/api/jobs/${job.id}/timeline`); } catch (error) { /* no scenes yet */ } }

  const starts = [
    h("div", {class: "row"}, h("div", {class: "cell"}, h("h1", {class: "title"}, "Co chcesz zrobić?"),
      h("p", {class: "sub"}, "Wybierz rodzaj filmu. Kreator zapyta o folder z nagraniami i zrobi resztę."))),
    h("div", {class: "row c4"}, STARTS.map(([key, glyph, title, text]) => h("a", {class: "cell", href: `#nowy/${key}`},
      h("div", {class: "entry"}, icon(glyph, "icon glyph"), h("div", {}, h("div", {class: "h3"}, title, icon("right")), h("p", {}, text)))))),
    h("div", {class: "gap"})];

  if (!job) {
    view.replaceChildren(...starts,
      h("div", {class: "row"}, h("div", {class: "cell empty"}, icon("film", "icon glyph"),
        h("h2", {class: "title"}, "Nie ma jeszcze żadnego filmu"),
        h("p", {}, "Wskaż folder z nagraniami z kamery 360°. Aplikacja sama znajdzie najlepsze momenty, wykadruje je i sklei w gotowy film."),
        h("a", {class: "btn primary big", href: "#nowy"}, icon("plus"), "Zrób pierwszy film"))),
      h("div", {class: "row wide"}, status(state), h("div", {class: "cell"}, h("h2", {class: "title"}, "Jak to działa"),
        h("ol", {class: "facts", style: "padding-left:18px;list-style:decimal"},
          h("li", {}, h("b", {}, "Nagrania. "), "Wybierasz folder z plikami z kamery."),
          h("li", {}, h("b", {}, "Co ma powstać. "), "Zaznaczasz, jakie filmy chcesz."),
          h("li", {}, h("b", {}, "Start. "), "Aplikacja pracuje sama, a film czeka w zakładce „Filmy”.")))));
    return;
  }

  const [cell, draw] = current(job, data, films);
  view.replaceChildren(...starts,
    h("div", {class: "row wide"}, cell, h("div", {class: "stack"}, status(state), latest(films))),
    h("div", {class: "gap"}),
    h("div", {class: "row"}, h("div", {class: "cell"}, h("div", {class: "spread"},
      h("div", {}, h("h2", {class: "title"}, "Gotowe filmy"), h("p", {class: "sub"}, films.length ? `${plural(films.length, "film", "filmy", "filmów")}, najnowsze na początku` : "Jeszcze żadnego. Pierwszy pojawi się tutaj po renderze.")),
      films.length > 12 && h("a", {class: "btn", href: "#filmy"}, "Wszystkie filmy")))),
    films.length ? h("div", {class: "row auto"}, grid(films)) : null);

  if (draw) draw();
  const observer = draw && new ResizeObserver(draw);
  if (observer) observer.observe(cell);
  let last = job.status;
  const stop = watch(list => {                   // the words follow the work; the page is rebuilt only when a film is finished
    const now = list.find(j => j.id === job.id);
    const state = view.querySelector("#home-state");
    if (now && state) state.textContent = subtitle(now, data);
    if (now && now.status !== last) { last = now.status; window.dispatchEvent(new HashChangeEvent("hashchange")); }
  });
  return () => { stop(); if (observer) observer.disconnect(); };
}
