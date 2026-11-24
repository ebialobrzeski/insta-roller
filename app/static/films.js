// Moje nagrania: every folder of recordings, what is happening to it, and the reels made from it.
// The reels are what a person comes for, so they are on top; everything else is under "Więcej".
import {h, api, icon, guard, toast, size, date, duration, plural, stateText, problemText, skipText, lastSaid, fileUrl, inWindow, reveal, saveButton} from "./ui.js";
import {live, watch, refresh, setContext} from "./live.js";
import * as review from "./review.js";

const PARTS = ["Szukam najlepszych momentów", "Układam rolki", "Robię rolki"];
const act = (job, path, body) => guard(async () => { await api(`/api/jobs/${job.id}/${path}`, "POST", body); await refresh(); });

function progress(job) {
  if (job.status !== "running" && job.status !== "queued") return null;
  const steps = job.steps.length || 1, inStep = Math.min(Math.max(job.progress * steps - job.step_index, 0), 1);
  const shot = job.detail ? job.detail.shot / Math.max(job.detail.of, 1) : inStep;
  return h("div", {class: "steps3", role: "progressbar", "aria-valuemin": 0, "aria-valuemax": 100, "aria-valuenow": Math.round(job.progress * 100), "aria-valuetext": stateText(job)},
    PARTS.map((name, i) => h("div", {class: job.status === "queued" ? "" : i < job.phase ? "done" : i === job.phase ? "on" : "",
                                     style: i === job.phase ? `--p:${Math.round(Math.max(shot, .06) * 100)}%` : null}, name)));
}

// The part of a card that changes while the work goes on: the name, the state, the one thing to do next.
function head(job) {
  const busy = job.status === "running" || job.status === "queued";
  const [what, hint] = job.status === "error" ? problemText(job) : [];
  const tag = {done: "tag good", error: "tag bad", review: "tag", cancelled: "tag plain", queued: "tag plain"}[job.status] || "tag";
  return h("div", {class: "job-head"},
    h("div", {class: "head"},
      h("div", {style: "min-width:0"},
        h("h2", {class: "title"}, job.label, job.status === "running" && h("span", {class: "live"}, "[ W TOKU ]")),
        h("p", {class: "sub mono", style: "overflow-wrap:anywhere;font-size:12px"}, job.folder)),
      h("div", {class: "state", style: "justify-content:flex-end;text-align:right"},
        h("span", {class: tag}, stateText(job)),
        h("span", {class: "mono muted"}, busy ? duration(job.elapsed_s) : date(job.updated)))),
    progress(job),
    job.status === "running" && h("p", {class: "alive"}, h("i", {"aria-hidden": "true"}), h("span", {}, lastSaid(job) || "Pracuję…"),
      h("span", {class: "muted"}, ` · trwa ${duration(job.elapsed_s)}`)),
    job.status === "queued" && h("p", {class: "alive idle"}, h("span", {class: "muted"}, "Czeka, aż skończy się poprzednia praca.")),
    what && h("div", {class: "problem"}, h("b", {}, what), hint),
    (busy || job.status === "error" || job.status === "cancelled" || job.status === "review") && h("div", {class: "inline mt"},
      busy && h("button", {class: "btn", type: "button", onclick: act(job, "cancel")}, "Zatrzymaj"),
      (job.status === "error" || job.status === "cancelled") && h("button", {class: "btn primary", type: "button", onclick: act(job, "retry")}, "Spróbuj ponownie"),
      job.status === "review" && h("a", {class: "btn primary big", href: `#filmy/${job.id}/ujecia`}, icon("eye"), "Sprawdź ujęcia i zrób rolki"),
      job.status === "review" && h("span", {class: "muted", style: "font-size:13px"}, "Najlepsze momenty są wybrane. Zobacz je, popraw, co chcesz, i uruchom robienie rolek.")));
}

// The part that changes only when files appear: the reels, then everything else under "Więcej".
function body(job, r) {
  const length = h("input", {type: "number", min: 5, step: 1, placeholder: "sekundy", style: "max-width:120px", "aria-label": "Długość nowej wersji w sekundach"});
  const idle = job.status !== "running" && job.status !== "queued";
  const reel = f => h("div", {class: "film"},
    h("video", {controls: true, preload: "metadata", playsinline: true, src: fileUrl(job.id, f.name)}),
    h("div", {}, h("div", {class: "name"}, f.title), h("div", {class: "tags", style: "margin-top:6px"},
      f.length_s && h("span", {class: "tag"}, `${f.length_s} s`), h("span", {class: "tag plain"}, size(f.size)), h("span", {class: "tag plain"}, date(f.mtime)))),
    saveButton(fileUrl(job.id, f.name, true), f.path));
  const wide = job.kind === "holiday" ? "films wide" : "films";
  // The reels of the present plan come first. While a new run is going, the ones made before stay in view.
  const fresh = r.films.filter(f => f.current);
  const now = fresh.length ? fresh : r.films, old = r.films.filter(f => !now.includes(f));
  const waiting = job.status !== "done" && now.length > 0;
  return h("div", {class: "job-body"},
    now.length ? h("div", {class: "mt2"},
      h("div", {class: "h3"}, waiting ? `Rolki zrobione wcześniej (${now.length})` : plural(now.length, "rolka", "rolki", "rolek")),
      h("div", {class: wide, style: "margin-top:12px"}, now.map(reel)))
      : idle && job.status === "done" ? h("p", {class: "muted mt2"}, "Nie powstała żadna rolka. Powody są pod „Więcej”.") : null,
    idle && r.scenes ? h("div", {class: "inline mt2"},
      job.status !== "review" && h("a", {class: "btn", href: `#filmy/${job.id}/ujecia`}, icon("eye"), "Popraw ujęcia"),
      h("a", {class: "btn", href: `#nowy/z/${job.id}`}, icon("plus"), "Zrób inne rolki z tego nagrania")) : null,
    h("details", {class: "mt2"}, h("summary", {}, "Więcej"),
      old.length ? h("div", {class: "mt"}, h("div", {class: "h3"}, `Starsze wersje (${old.length})`),
        h("div", {class: wide, style: "margin-top:12px"}, old.map(reel))) : null,
      r.shots.length ? h("div", {class: "mt2"}, h("div", {class: "h3"}, "Osobne ujęcia, do własnego montażu"),
        h("div", {class: "inline", style: "margin-top:8px"}, r.shots.map(s => inWindow
          ? h("button", {class: "btn small", type: "button", onclick: () => reveal(s.path)}, icon("folder"), `${s.title}${s.length_s ? `, ${s.length_s} s` : ""}`)
          : h("a", {class: "btn small", href: `/api/jobs/${job.id}/zip/${encodeURIComponent(s.folder)}`}, icon("down"), `${s.title}${s.length_s ? `, ${s.length_s} s` : ""} (zip)`)))) : null,
      idle && r.scenes ? h("div", {class: "mt2"}, h("div", {class: "h3"}, "Te same rolki w innej długości"),
        h("div", {class: "inline", style: "margin-top:8px"}, length,
          h("button", {class: "btn", type: "button", onclick: guard(async () => {
            if (!+length.value) throw new Error("Podaj długość w sekundach");
            await api(`/api/jobs/${job.id}/version`, "POST", {length: +length.value});
            toast("Robię nową wersję. Momenty są już znalezione, więc pójdzie szybciej.");
            await refresh();
          })}, "Zrób"),
          h("span", {class: "muted", style: "font-size:13px"}, "Te same momenty, inna długość każdej rolki."))) : null,
      r.skipped.length ? h("div", {class: "mt2"}, h("div", {class: "h3"}, "Rolki, które nie powstały"),
        h("ul", {class: "facts"}, r.skipped.map(s => h("li", {}, h("b", {}, `${s.title || s.theme || s.tag}: `), skipText(s.reason))))) : null,
      h("div", {class: "mt2"}, h("div", {class: "h3"}, "Gdzie są pliki"),
        h("p", {class: "mono", style: "font-size:13px;margin-top:6px;overflow-wrap:anywhere"}, job.out),
        inWindow && h("button", {class: "btn small", type: "button", style: "margin-top:8px", onclick: () => reveal(job.out)}, icon("folder"), "Otwórz folder z rolkami")),
      h("details", {class: "mt2"}, h("summary", {}, "Szczegóły techniczne (dla ciekawych i do zgłaszania błędów)"),
        r.report && h("p", {style: "margin-top:8px"}, h("a", {href: fileUrl(job.id, "scenes_report.txt"), target: "_blank"}, "Lista znalezionych momentów (plik tekstowy)")),
        job.error && h("div", {class: "muted", style: "font-size:13px;margin-top:10px"}, "Komunikat błędu"),
        job.error && h("pre", {}, job.error),
        h("div", {class: "muted", style: "font-size:13px;margin-top:10px"}, "Zapis pracy"),
        h("pre", {class: "log"}, job.log || "(brak zapisu)"),
        h("div", {class: "muted", style: "font-size:13px;margin-top:10px"}, "Polecenia"),
        h("pre", {}, job.commands.join("\n"))),
      idle && h("div", {class: "inline mt2"},
        h("button", {class: "btn small danger", type: "button", onclick: guard(async () => {
          if (!confirm(`Usunąć „${job.label}” z listy? Nagrania i gotowe rolki zostaną na dysku.`)) return;
          await api(`/api/jobs/${job.id}`, "DELETE");
          await refresh();
        })}, "Usuń z listy"),
        h("span", {class: "muted", style: "font-size:13px"}, "Znika tylko wpis na tej liście. Nagrania i rolki zostają na dysku."))));
}

const headKey = j => JSON.stringify([j.status, j.stage, j.detail, Math.round(j.progress * 100), j.cancel, j.error, j.label, j.status === "running" ? [Math.floor(j.elapsed_s), lastSaid(j)] : 0]);
const bodyKey = j => JSON.stringify([j.status, j.step_index, j.queued]);

export async function show(view, [id, sub]) {
  if (id && sub === "ujecia") return review.show(view, +id);
  setContext("Moje nagrania");
  const cards = new Map();                       // job id -> {cell, head, body, keys}

  async function sync(jobs) {
    if (!jobs.length) {
      cards.clear();
      view.replaceChildren(h("div", {class: "row"}, h("div", {class: "cell empty"}, icon("film", "icon glyph"),
        h("h1", {class: "title"}, "Tu będą Twoje nagrania i rolki"),
        h("p", {}, "Każdy lot albo wyjazd to jeden folder z nagraniami. Zobaczysz tu, co aplikacja z nim robi, a potem gotowe rolki do obejrzenia i zapisania."),
        h("a", {class: "btn primary big", href: "#nowy"}, icon("plus"), "Zrób pierwsze rolki"))));
      return;
    }
    if (!cards.size) view.replaceChildren(h("div", {class: "row"}, h("div", {class: "cell"}, h("h1", {class: "title"}, "Moje nagrania"),
      h("p", {class: "sub"}, "Każdy lot albo wyjazd osobno, z rolkami, które z niego powstały. Najnowsze na górze."))), h("div", {class: "gap"}));
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
