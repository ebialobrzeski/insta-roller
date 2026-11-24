// Przegląd: the one thing that is going on or waits for the person, as a picture; then the latest reels.
import {h, api, icon, size, date, duration, plural, shotName, stateText, problemText, lastSaid, fileUrl, clock, THEMES} from "./ui.js";
import {live, watch, setContext, play} from "./live.js";

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
    h("text", {x: W - 2, y: Math.max(y(hi) - 4, 10), "text-anchor": "end"}, `${Math.round(hi)} m`),
    h("line", {class: "cursor", x1: 0, x2: 0, y1: pad.t, y2: base, visibility: "hidden"}),
    h("circle", {class: "cursor-dot", r: 5, visibility: "hidden"})));

  // Read-out: what the flight was doing at the moment under the pointer (or the arrow keys).
  const svg = box.querySelector("svg"), cursor = svg.querySelector(".cursor"), dot = svg.querySelector(".cursor-dot");
  const tip = h("div", {class: "readout", role: "status", hidden: true});
  box.append(tip);
  svg.setAttribute("tabindex", "0");
  const signed = v => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(1).replace(".", ",")}`;
  let at = null;
  function show(t) {
    at = Math.min(Math.max(t, t0), t1);
    let i = points.findIndex(p => p[0] >= at);
    if (i < 0) i = points.length - 1;
    if (i > 0 && at - points[i - 1][0] < points[i][0] - at) i -= 1;
    const [, , speed, vario] = points[i], alt = altAt(at), px = x(at);
    const scene = (data.scenes || []).find(sc => at >= sc.start_s && at <= sc.end_s && sc.type !== "release")
      || (data.scenes || []).find(sc => at >= sc.start_s && at <= sc.end_s);
    const used = data.shots.filter(sh => at >= sh.start_s && at <= sh.start_s + sh.duration_s);
    const films = [...new Set(used.map(sh => { const [, base, part] = /^(.*?)(?:_p(\d+))?$/.exec(sh.theme || sh.reel); return (THEMES[base] || [base])[0] + (part ? ` ${part}` : ""); }))];
    cursor.setAttribute("x1", px); cursor.setAttribute("x2", px); cursor.setAttribute("visibility", "visible");
    dot.setAttribute("cx", px); dot.setAttribute("cy", y(alt)); dot.setAttribute("visibility", "visible");
    tip.replaceChildren(...[
      scene && scene.thumb ? h("img", {alt: "", src: fileUrl(data.job, `review/${scene.id}.jpg`)}) : null,
      h("div", {},
        h("div", {class: "when"}, label(at - t0), scene && h("b", {}, shotName(scene.type, scene.attrs))),
        h("dl", {},
          h("dt", {}, "Wysokość"), h("dd", {}, `${Math.round(alt)} m`),
          speed != null && h("dt", {}, "Prędkość"), speed != null && h("dd", {}, `${Math.round(speed)} km/h`),
          vario != null && h("dt", {}, vario >= 0 ? "Wznoszenie" : "Opadanie"), vario != null && h("dd", {class: vario >= 0.2 ? "up" : vario <= -0.2 ? "down" : ""}, `${signed(vario)} m/s`)),
        h("div", {class: films.length ? "in" : "in none"}, films.length ? `Ujęcie w: ${films.join(", ")}` : "Ten moment nie trafił do żadnej rolki"))].filter(Boolean));
    tip.hidden = false;
    // Beside the cursor, never on it: to the right while there is room, else to the left;
    // and on the half of the chart where the line is not (the line is high: the read-out sits low).
    const width = tip.offsetWidth, height = tip.offsetHeight, left = px / W * box.clientWidth, gap = 16;
    const room = box.clientWidth - left - gap >= width;
    tip.style.left = `${Math.max(room ? left + gap : left - gap - width, 0)}px`;
    const plot = svg.getBoundingClientRect().height, lineY = y(alt) / H * plot;
    tip.style.top = `${lineY < plot / 2 ? Math.max(plot - height - 34, 0) : 0}px`;
  }
  function hide() { at = null; tip.hidden = true; cursor.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); }
  const timeAt = e => { const r = svg.getBoundingClientRect(); return t0 + (e.clientX - r.left) / r.width * (t1 - t0); };
  svg.addEventListener("pointermove", e => show(timeAt(e)));
  svg.addEventListener("pointerdown", e => show(timeAt(e)));
  svg.addEventListener("pointerleave", e => { if (e.pointerType === "mouse" && document.activeElement !== svg) hide(); });
  svg.addEventListener("blur", hide);
  svg.addEventListener("keydown", e => {
    const step = (t1 - t0) / (e.shiftKey ? 20 : 120);
    const marks = shots.map(sh => sh.start_s + sh.duration_s / 2).sort((a, b) => a - b), now = at == null ? t0 : at;
    const to = {ArrowRight: now + step, ArrowLeft: now - step, Home: t0, End: t1,
                PageDown: marks.find(m => m > now + 1), PageUp: [...marks].reverse().find(m => m < now - 1), Escape: null}[e.key];
    if (e.key === "Escape") return hide();
    if (to === undefined) return;
    e.preventDefault();
    show(to);
  });
}

function dayStrip(data) {
  const longest = Math.max(...data.days.map(d => d.duration_s), 1);
  return h("div", {class: "days", role: "img", "aria-label": `Nagrania z ${plural(data.days.length, "dnia", "dni", "dni")}`},
    data.days.map(d => h("div", {style: `height:${Math.max(d.duration_s / longest * 100, 5)}%`, title: `${d.label}: ${plural(d.clips, "nagranie", "nagrania", "nagrań")}, ${duration(d.duration_s)}`},
      h("b", {}, d.clips), h("span", {}, d.label.slice(0, 5)))));
}

// What a person should do or know right now, for the most recent recordings.
const busyText = job => job.status === "queued" ? "Czeka w kolejce." : `${stateText(job).replace(/…$/, "")}: ${lastSaid(job) || "pracuję"} (trwa ${duration(job.elapsed_s)}). Możesz w tym czasie robić coś innego.`;
function nextStep(job, reels) {
  const busy = job.status === "running" || job.status === "queued";
  const [what, hint] = job.status === "error" ? problemText(job) : [];
  if (job.status === "review") return ["Wybrałem najlepsze momenty. Sprawdź je, zanim zacznę robić rolki.",
    h("a", {class: "btn primary big", href: `#filmy/${job.id}/ujecia`}, icon("eye"), "Sprawdź ujęcia i zrób rolki")];
  if (what) return [`${what} ${hint}`, h("a", {class: "btn primary", href: `#filmy/${job.id}`}, "Zobacz, co się stało")];
  if (busy) return [busyText(job), h("a", {class: "btn", href: `#filmy/${job.id}`}, "Zobacz postęp")];
  if (job.status === "cancelled") return ["Praca została zatrzymana.", h("a", {class: "btn", href: `#filmy/${job.id}`}, "Zobacz szczegóły")];
  return [reels ? `Gotowe: ${plural(reels, "rolka", "rolki", "rolek")} do obejrzenia poniżej.` : "Gotowe, ale żadna rolka nie powstała.",
    h("a", {class: "btn", href: `#nowy/z/${job.id}`}, icon("plus"), "Zrób inne rolki z tego nagrania")];
}

function current(job, data, films) {
  const mine = films.filter(f => f.job === job.id);
  const busy = job.status === "running" || job.status === "queued";
  const flight = data && data.kind === "flight";
  const box = h("div", {class: "chart"});
  const [text, action] = nextStep(job, mine.length);
  const cell = h("div", {class: "cell"},
    h("div", {class: "head"},
      h("div", {style: "min-width:0"},
        h("h2", {class: "title"}, job.label, busy && h("span", {class: "live"}, "[ W TOKU ]")),
        h("p", {class: "sub", id: "home-state"}, subtitle(job, data)))),
    h("div", {class: "next"}, h("p", {id: "home-next"}, text), action),
    !data ? null : flight ? box : dayStrip(data),
    flight && h("p", {class: "muted", style: "font-size:13px;margin-top:10px"}, "Wysokość w czasie lotu. Pasy i kółka to momenty wybrane do rolek. Najedź na wykres, żeby zobaczyć, co działo się w danej chwili."));
  return [cell, flight ? () => barogram(box, data) : null];
}

function subtitle(job, data) {
  const kind = data && data.kind === "holiday" ? plural(data.days.length, "dzień", "dni", "dni") : data ? duration(data.points.length ? data.points[data.points.length - 1][0] - data.points[0][0] : 0) : "";
  return [kind, job.folder].filter(Boolean).join(" · ");
}

function grid(films) {
  return films.slice(0, 12).map(f => h("button", {class: "cell tight", type: "button", onclick: () => play(filmOf(f)), "aria-label": `Odtwórz: ${f.title}, ${f.job_label || f.job_name}`},
    h("div", {class: "spread", style: "flex-wrap:nowrap"},
      h("div", {style: "min-width:0"}, h("div", {class: "h3"}, f.title), h("div", {class: "muted", style: "overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px"}, f.job_label || f.job_name)),
      h("span", {class: "round", "aria-hidden": "true"}, icon("play"))),
    h("div", {class: "tags", style: "margin-top:12px"},
      f.length_s && h("span", {class: "tag"}, `${f.length_s} s`), h("span", {class: "tag plain"}, date(f.mtime)), h("span", {class: "tag plain"}, size(f.size)))));
}

export async function show(view) {
  setContext("Przegląd");
  const every = await api("/api/films");
  const films = every.some(f => f.current) ? every.filter(f => f.current) : every;      // not the earlier versions of other lengths
  const jobs = live.jobs;
  const job = jobs.find(j => j.status === "review") || jobs.find(j => j.status === "running") || jobs[0];    // what waits for the person comes first
  let data = null;
  if (job) { try { data = await api(`/api/jobs/${job.id}/timeline`); } catch (error) { /* no scenes yet */ } }

  if (!job) {
    view.replaceChildren(
      h("div", {class: "row"}, h("div", {class: "cell empty"}, icon("film", "icon glyph"),
        h("h1", {class: "title"}, "Zrób rolki z nagrań kamery 360°"),
        h("p", {}, "Wskaż folder z nagraniami z lotu albo z wyjazdu. Aplikacja sama znajdzie najlepsze momenty, pokaże Ci je do sprawdzenia i zrobi z nich gotowe rolki."),
        h("a", {class: "btn primary big", href: "#nowy"}, icon("plus"), "Zacznij: wskaż nagrania"))),
      h("div", {class: "row c3"},
        [["1", "Nagrania", "Wybierasz folder z plikami z kamery."], ["2", "Rolki", "Zaznaczasz, jakie rolki chcesz i ile."],
         ["3", "Ujęcia", "Oglądasz wybrane momenty, poprawiasz, co chcesz, i uruchamiasz robienie rolek."]]
          .map(([n, title, text]) => h("div", {class: "cell"}, h("div", {class: "h3"}, `${n}. ${title}`), h("p", {class: "muted", style: "margin-top:6px;font-size:14px"}, text)))));
    return;
  }

  const [cell, draw] = current(job, data, films);
  const others = jobs.filter(j => j.id !== job.id);
  view.replaceChildren(
    h("div", {class: "row"}, cell),
    h("div", {class: "gap"}),
    h("div", {class: "row"}, h("div", {class: "cell"}, h("div", {class: "spread"},
      h("div", {}, h("h2", {class: "title"}, "Ostatnie rolki"), h("p", {class: "sub"}, films.length ? "Kliknij, żeby obejrzeć. Wszystkie znajdziesz w „Moje nagrania”." : "Jeszcze żadnej. Pierwsze pojawią się tutaj, gdy będą gotowe.")),
      h("div", {class: "inline"},
        others.length > 0 && h("a", {class: "btn", href: "#filmy"}, `Moje nagrania (${jobs.length})`),
        h("a", {class: "btn primary", href: "#nowy"}, icon("plus"), "Nowe rolki"))))),
    films.length ? h("div", {class: "row auto"}, grid(films)) : null);

  if (draw) draw();
  const observer = draw && new ResizeObserver(draw);
  if (observer) observer.observe(cell);
  let last = job.status;
  const stop = watch(list => {                   // the words follow the work; the page is rebuilt only when the state changes
    const now = list.find(j => j.id === job.id);
    const state = view.querySelector("#home-state"), next = view.querySelector("#home-next");
    if (now && state) state.textContent = subtitle(now, data);
    if (now && next && now.status === "running") next.textContent = busyText(now);
    if (now && now.status !== last) { last = now.status; window.dispatchEvent(new HashChangeEvent("hashchange")); }
  });
  return () => { stop(); if (observer) observer.disconnect(); };
}
