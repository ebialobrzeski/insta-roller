// Osoby: who the app recognises in holiday footage, and refreshing the list from Immich.
import {h, api, icon, guard, toast, plural} from "./ui.js";
import {setContext} from "./live.js";

const ENV = [["IMMICH_TAILSCALE_ADDRESS", "Adres Immicha przez Tailscale"], ["IMMICH_ADDRESS", "Adres Immicha w sieci domowej"], ["IMMICH_API_KEY", "Klucz dostępu"]];
let names = null;                                // the list being edited; null = take the saved one

export async function show(view) {
  setContext("Osoby");
  let timer = null, alive = true;

  async function draw() {
    const p = await api("/api/people");
    if (!alive) return;
    if (names === null) names = [...p.names];
    const running = p.refresh.status === "running";
    const ready = p.env.IMMICH_API_KEY && (p.env.IMMICH_TAILSCALE_ADDRESS || p.env.IMMICH_ADDRESS);
    const add = h("input", {type: "text", placeholder: "Imię jak w Immichu", autocomplete: "off", "aria-label": "Dodaj osobę",
                            onkeydown: e => { if (e.key === "Enter") push(); }});
    const push = () => { const name = add.value.trim(); if (name && !names.includes(name)) { names.push(name); draw(); } };
    const exported = p.exported_at ? new Date(p.exported_at).toLocaleString("pl-PL", {day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit"}) : "";

    view.replaceChildren(
      h("div", {class: "row"}, h("div", {class: "cell"}, h("h1", {class: "title"}, "Osoby"),
        h("p", {class: "sub"}, "Te osoby aplikacja rozpoznaje w nagraniach z urlopu i kadruje tak, żeby było je widać. Wzory twarzy pochodzą z Twojej biblioteki zdjęć Immich."))),
      h("div", {class: "row wide"},
        h("div", {class: "cell flush"},
          h("div", {style: "padding:28px 32px 16px"}, h("h2", {class: "title"}, p.present ? plural(p.people.length, "osoba", "osoby", "osób") : "Brak listy osób"),
            h("p", {class: "sub"}, p.present ? (exported ? `Pobrane z Immicha ${exported}` : "Lista jest zapisana na tym komputerze") : "Pobierz ją z Immicha przyciskiem obok. Bez listy filmy powstają tak samo, tylko bez kadrowania na osoby.")),
          h("ul", {class: "list"}, p.people.map(person => h("li", {},
            h("span", {class: "round", "aria-hidden": "true", style: "cursor:default"}, person.name.slice(0, 1).toUpperCase()),
            h("div", {class: "grow"}, h("div", {class: "name"}, person.name)),
            h("span", {class: "tag"}, plural(person.samples, "wzór twarzy", "wzory twarzy", "wzorów twarzy")))))),
        h("div", {class: "stack"},
          h("div", {class: "cell"},
            h("h2", {class: "title"}, "Odśwież z Immicha"), h("p", {class: "sub"}, "Pobiera aktualne wzory twarzy wybranych osób. Niczego w Immichu nie zmienia."),
            h("div", {class: "field mt"}, "Kogo pobrać"),
            h("div", {class: "checks", style: "margin-top:8px"}, names.map(name => h("span", {class: "chip", style: "font-size:14px;padding:4px 6px 4px 10px"}, name,
              h("button", {type: "button", "aria-label": `Usuń: ${name}`, onclick: () => { names = names.filter(n => n !== name); draw(); }}, "×")))),
            h("div", {class: "inline", style: "margin-top:8px"}, add, h("button", {class: "btn", type: "button", onclick: push}, "Dodaj")),
            h("div", {class: "inline mt2"},
              h("button", {class: "btn primary", type: "button", disabled: running || !ready || !names.length, onclick: guard(async () => {
                await api("/api/people/refresh", "POST", {names});
                toast("Pobieram wzory twarzy z Immicha");
                draw();
              })}, icon("refresh"), running ? "Pobieram…" : "Odśwież z Immicha"),
              p.refresh.status === "done" && h("span", {class: "tag good"}, "Gotowe"),
              p.refresh.status === "error" && h("span", {class: "tag bad"}, "Nie udało się")),
            p.refresh.status === "error" && h("div", {class: "problem"}, h("b", {}, "Nie udało się pobrać osób z Immicha."),
              "Sprawdź, czy Immich jest włączony i osiągalny z tego komputera oraz czy klucz dostępu jest aktualny. Dokładny komunikat jest w szczegółach poniżej."),
            p.refresh.log.length ? h("details", {class: "mt"}, h("summary", {}, "Szczegóły techniczne"), h("pre", {}, p.refresh.log.join("\n"))) : null),
          h("div", {class: "cell"},
            h("h2", {class: "title"}, "Połączenie"), h("p", {class: "sub"}, "Adres i klucz są w zmiennych środowiska albo w pliku .env w folderze aplikacji. Klucza nigdzie nie pokazuję."),
            h("dl", {class: "kv"}, ENV.map(([key, text]) => [h("dt", {}, text, h("div", {class: "mono", style: "font-size:11px"}, key)),
              h("dd", {}, h("span", {class: p.env[key] ? "tag good" : "tag plain"}, p.env[key] ? "ustawiony" : "brak"))])),
            !ready && h("p", {class: "muted mt", style: "font-size:13px"}, "Żeby pobrać osoby, potrzebny jest klucz dostępu i przynajmniej jeden adres.")))));
    clearTimeout(timer);
    if (running) timer = setTimeout(draw, 1500);
  }

  await draw();
  return () => { alive = false; clearTimeout(timer); };
}
