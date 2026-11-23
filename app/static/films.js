// Filmy: every film project, what is happening to it, and its finished films.
import {h, api, icon, guard, toast, size, date, duration, stateText, problemText, fileUrl} from "./ui.js";
import {live, watch, refresh, setContext} from "./live.js";
import * as review from "./review.js";

const PARTS = ["Szukanie scen", "Plan", "Render"];
const act = (job, path, body) => guard(async () => { await api(`/api/jobs/${job.id}/${path}`, "POST", body); await refresh(); });

function progress(job) {
  if (job.status !== "running" && job.status !== "queued") return null;
  const steps = job.steps.length || 1, inStep = Math.min(Math.max(job.progress * steps - job.step_index, 0), 1);
  const shot = job.detail ? job.detail.shot / Math.max(job.detail.of, 1) : inStep;
  return h("div", {class: "steps3", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(job.progress * 100), "aria-valuetext": stateText(job)},
    PARTS.map((name, i) => h("div", {class: job.status === "queued" ? "" : i < job.phase ? "done" : i === job.phase ? "on" : "",
                                     style: i === job.phase ? `--p:${Math.round(Math.max(shot, .06) * 100)}%` : null}, name)));
}

// The part of a card that changes while the job runs.
function head(job) {
  const busy = job.status === "running" || job.status === "queued";
  const [what, hint] = job.status === "error" ? problemText(job) : [];
  const tag = {done: "tag good", error: "tag bad", review: "tag", cancelled: "tag plain", queued: "tag plain"}[job.status] || "tag";
  return h("div", {class: "job-head"},
    h("div", {class: "head"},
      h("div", {style: "min-width:0"},
        h("h2", {class: "title"}, job.name, job.status === "running" && h("span", {class: "live"}, "[ W TOKU ]")),
        h("p", {class: "sub mono", style: "overflow-wrap:anywhere"}, job.folder)),
      h("div", {class: "state", style: "justify-content:flex-end;text-align:right"},
        h("span", {class: tag}, stateText(job)),
        h("span", {class: "mono muted"}, busy ? duration(job.elapsed_s) : date(job.updated)))),
    progress(job),
    what && h("div", {class: "problem"}, h("b", {}, what), hint),
    h("div", {class: "inline mt"},
      busy && h("button", {class: "btn", type: "button", onclick: act(job, "cancel")}, "Zatrzymaj"),
      (job.status === "error" || job.status === "cancelled") && h("button", {class: "btn primary", type: "button", onclick: act(job, "retry")}, "Spróbuj ponownie"),
      job.status === "review" && h("a", {class: "btn primary", href: `#filmy/${job.id}/ujecia`}, icon("eye"), "Przejrzyj ujęcia"),
      job.status === "review" && h("span", {class: "muted", style: "font-size:13px"}, "Ujęcia są wybrane. Zobacz je, popraw i uruchom render.")));
}

// The part that changes only when files appear: films, shots, the technical details.
function body(job, r) {
  const length = h("input", {type: "number", min: 5, step: 1, placeholder: "sekundy", style: "max-width:120px", "aria-label": "Długość nowej wersji w sekundach"});
  const idle = job.status !== "running" && job.status !== "queued";
  const film = f => h("div", {class: "film"},
    h("video", {controls: true, preload: "metadata", playsinline: true, src: fileUrl(job.id, f.name)}),
    h("div", {}, h("div", {class: "name"}, f.title), h("div", {class: "tags", style: "margin-top:6px"},
      f.length_s && h("span", {class: "tag"}, `${f.length_s} s`), h("span", {class: "tag plain"}, size(f.size)), h("span", {class: "tag plain"}, date(f.mtime)))),
    h("a", {class: "btn small", href: fileUrl(job.id, f.name, true)}, icon("down"), "Pobierz"));
  const wide = job.kind === "holiday" ? "films wide" : "films";
  // Films of the present plan are shown; other lengths, and everything while a new run is not finished, are "earlier".
  const fresh = job.status === "done" ? r.films.filter(f => f.current) : [];
  const now = job.status !== "done" ? [] : fresh.length ? fresh : r.films, old = r.films.filter(f => !now.includes(f));
  return h("div", {class: "job-body"},
    now.length ? h("div", {class: wide}, now.map(film)) : null,
    old.length ? h("details", {class: "mt"}, h("summary", {}, `Wcześniejsze filmy z tego folderu (${old.length})`), h("div", {class: wide}, old.map(film))) : null,
    r.shots.length ? h("div", {class: "inline mt2"}, r.shots.map(s =>
      h("a", {class: "btn small", href: `/api/jobs/${job.id}/zip/${encodeURIComponent(s.folder)}`}, icon("down"), `Pobierz ujęcia (zip): ${s.title}${s.length_s ? `, ${s.length_s} s` : ""}`))) : null,
    idle && r.scenes ? h("div", {class: "inline mt2"},
      job.status === "done" && h("a", {class: "btn", href: `#filmy/${job.id}/ujecia`}, icon("eye"), "Popraw ujęcia"),
      h("details", {style: "flex:1 1 280px"}, h("summary", {}, "Nowa wersja o innej długości"),
        h("div", {class: "inline", style: "margin-top:8px"}, length,
          h("button", {class: "btn", type: "button", onclick: guard(async () => {
            if (!+length.value) throw new Error("Podaj długość nowej wersji w sekundach");
            await api(`/api/jobs/${job.id}/version`, "POST", {length: +length.value});
            toast("Robię nową wersję. Sceny są już znalezione, więc pójdzie szybciej.");
            await refresh();
          })}, "Zrób"),
          h("span", {class: "muted", style: "font-size:13px"}, "Te same sceny, inna długość. Bez ponownego szukania scen.")))) : null,
    h("details", {class: "mt"}, h("summary", {}, "Szczegóły techniczne"),
      h("dl", {class: "kv", style: "margin-top:8px"}, h("dt", {}, "Filmy są w"), h("dd", {class: "mono"}, job.out)),
      r.skipped.length ? h("p", {class: "muted", style: "font-size:13px;margin-top:8px"}, "Bez filmu: ", r.skipped.map(s => `${s.tag} (${s.reason})`).join("; ")) : null,
      r.report && h("p", {style: "margin-top:8px"}, h("a", {href: fileUrl(job.id, "scenes_report.txt"), target: "_blank"}, "Lista znalezionych scen")),
      job.error && h("pre", {}, job.error),
      h("pre", {class: "log"}, job.log || "(brak zapisu)"),
      h("pre", {}, job.commands.join("\n")),
      idle && h("div", {class: "inline mt"},
        h("button", {class: "btn small danger", type: "button", onclick: guard(async () => {
          if (!confirm(`Usunąć „${job.name}” z listy? Nagrania i gotowe filmy zostaną na dysku.`)) return;
          await api(`/api/jobs/${job.id}`, "DELETE");
          await refresh();
        })}, "Usuń z listy"),
        h("span", {class: "muted", style: "font-size:13px"}, "Znika tylko wpis na tej liście. Pliki zostają."))));
}

const headKey = j => JSON.stringify([j.status, j.stage, j.detail, Math.round(j.progress * 100), j.cancel, j.error, Math.floor(j.elapsed_s / 5)]);
const bodyKey = j => JSON.stringify([j.status, j.step_index, j.queued]);

export async function show(view, [id, sub]) {
  if (id && sub === "ujecia") return review.show(view, +id);
  setContext("Filmy");
  const cards = new Map();                       // job id -> {cell, head, body, keys}

  async function sync(jobs) {
    if (!jobs.length) {
      cards.clear();
      view.replaceChildren(h("div", {class: "row"}, h("div", {class: "cell empty"}, icon("film", "icon glyph"),
        h("h1", {class: "title"}, "Tu będą Twoje filmy"),
        h("p", {}, "Każdy folder z nagraniami to jeden projekt. Zobaczysz tu postęp pracy, a potem gotowe filmy do obejrzenia i pobrania."),
        h("a", {class: "btn primary big", href: "#nowy"}, icon("plus"), "Zrób pierwszy film"))));
      return;
    }
    if (!cards.size) view.replaceChildren(h("div", {class: "row"}, h("div", {class: "cell"}, h("h1", {class: "title"}, "Filmy"),
      h("p", {class: "sub"}, "Jeden projekt na folder z nagraniami. Najnowsze na górze."))), h("div", {class: "gap"}));
    for (const job of jobs) {
      let card = cards.get(job.id);
      if (!card) {
        card = {cell: h("div", {class: "cell", id: `film-${job.id}`}), head: null, body: h("div"), keys: []};
        card.row = h("div", {class: "row"}, card.cell);
        cards.set(job.id, card);
        view.append(card.row);
      }
      const keys = [headKey(job), bodyKey(job)];
      if (keys[0] !== card.keys[0]) {
        const next = head(job);
        if (card.head) card.head.replaceWith(next); else card.cell.prepend(next);
        card.head = next;
      } else if (job.status === "running") {       // the log grows without a new card
        const log = card.cell.querySelector("pre.log");
        if (log && log.textContent !== job.log) log.textContent = job.log;
      }
      if (keys[1] !== card.keys[1]) {
        card.keys = keys;
        const results = await api(`/api/jobs/${job.id}/results`).catch(() => ({films: [], shots: [], skipped: [], report: false}));
        results.scenes = results.report || job.status === "done" || job.status === "review" || results.films.length > 0;
        const next = body(job, results);
        card.body.replaceWith(next);
        if (!next.isConnected) card.cell.append(next);
        card.body = next;
      }
      card.keys = keys;
    }
    for (const [jobId, card] of cards) if (!jobs.some(j => j.id === jobId)) { card.row.remove(); cards.delete(jobId); }
  }

  let busy = false;
  const safe = async jobs => { if (busy) return; busy = true; try { await sync(jobs); } finally { busy = false; } };
  await sync(live.jobs);
  if (id) { const target = document.getElementById(`film-${id}`); if (target) target.scrollIntoView({block: "start"}); }
  return watch(safe);
}
