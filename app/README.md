# insta-roller: the application (stage 7)

One program on the Windows PC: a job queue, a job runner and a browser UI. The UI adds nothing of its own to the pipeline: every job is a list of ordinary
CLI steps (`python -m core.cli analyze|plan|render|shots`), run one after another as
subprocesses, and the "Filmy" screen shows the exact commands under "Szczegóły techniczne".

## Start

```
pip install -e .[app]
.venv\Scripts\python.exe -m app
```

or double-click `insta-roller.bat` in the repository root (the same command), or run the
`insta-roller-app` console script. This one process

- serves the UI on `http://127.0.0.1:8756/` (another free port when 8756 is taken) and opens it
  in the default browser,
- runs the queued jobs in a background thread, one at a time,
- stops on Ctrl+C: the running step is killed together with its FFmpeg processes and its job
  continues at that step the next time the app starts.

Starting it a second time on the same data folder only opens the browser at the running app.

| Option | Default | Meaning |
|---|---|---|
| `--no-browser` | | do not open the browser |
| `--port` | `8756` | port of the UI |
| `--host` | `127.0.0.1` | address to listen on. Anything else (e.g. `0.0.0.0`, to use the UI from a phone) lets every device that can reach the port queue jobs, edit profiles and download films: **there is no login** |
| `--data` | `%LOCALAPPDATA%\insta-roller` | the app's own folder: `queue.db` (SQLite) and `settings.json` |

## Folders

- **Job folder**: any folder on the machine. The picker on "Nowy film" starts at the drives,
  mapped network drives (a NAS share) included; a path, also a UNC one (`\\nas\wideo\lot`), can be
  typed or pasted. Jobs store absolute paths. Recordings are only read.
- **Folder wyników** (output folder, per job): `<job folder>\insta-roller` unless another folder is
  given in the form; it is passed to every step as `--out`. It cannot be the job folder itself
  (the films would be taken for recordings) or another job's output folder.
- **Folder pamięci podręcznej** (cache folder, on "Ustawienia"): a fast local disk for proxies and
  work files, passed as `--work <folder>\job_<id>`. Empty: the CLI's default, `<output folder>\.cache`.
- **Profiles**: the repository's `profiles/`, edited in place on "Ustawienia"; the next step that
  starts uses them. A job with profile "inne" carries its own settings in
  `<output folder>\job_profile.yaml`.

The app writes only into a job's output folder (edited reel plans, `job_profile.yaml`),
`profiles/`, the cache folder and its data folder.

## Queue

One row per job folder: adding a folder that already has a job reuses that job, the pipeline runs
again and the CLI processes only what is new. A job that fails can be retried from the failed
step; every step is cached by the CLI, so repeating one is cheap. A job that was running when the
app was closed, killed or the PC switched off goes back to the queue at the step it was in when
the app starts again. Cancelling a running job kills its step.

## The UI

Polish, plain HTML + CSS + ES modules in `static/` (no build step, no web fonts, nothing loaded
from the internet). A fixed sidebar, a hairline grid of cells, one orange accent; light and dark
theme (system / light / dark switch in the sidebar, remembered in the browser); usable on a phone.

| Screen | What it is for |
|---|---|
| Przegląd | four ways to start; the current film as a picture (a flight's altitude with the chosen shots on it, a holiday's days); what this PC has (stitcher, graphics card, disk space, people); the finished films |
| Nowy film | three steps: 1 the folder (what was found in it, in words; flight or holiday), 2 what to make (reel themes, or day / trip films; review first; separate shots), 3 quality and start. Profile, mount, length and a one-off profile are under "Więcej ustawień" |
| Filmy | one cell per job folder: state in words, three-part progress, the films to play and download, shots as zip, a new version of another length, "Usuń z listy" (the files stay). Raw log and the exact commands are under "Szczegóły techniczne" |
| Filmy → Przejrzyj ujęcia | the scene review: every film as a strip of shots; remove, shorten, lengthen, change the camera direction, move, add unused scenes; "Renderuj" |
| Osoby | who is in `people.json`; "Odśwież z Immicha" |
| Ustawienia | cache folder, default quality, theme; the YAML files under "Zaawansowane" |

## What the UI does, as CLI commands

`<folder>` is the job folder, `<opts>` the job's options.

| UI | Commands |
|---|---|
| Nowy film: "Zrób film" | `python -m core.cli analyze <folder> <opts>`, then `plan`, then `render` (same as `run`) |
| ... lot / urlop | `--profile flight` / `--profile holiday` when chosen by hand, else `--profile auto` |
| ... themes of a flight | `--enable landing,...` / `--disable thermal,...` (only differences from the theme files) |
| ... holiday: every day / the trip / both | `--mode day\|trip\|both` |
| ... dane lotu na obrazie | `--overlay` / `--no-overlay` |
| ... kogo szukać | `--people Ala,Olek` |
| ... przejrzeć ujęcia | `analyze`, `plan`, then `python -m app.thumbs <folder> <opts>`; the job waits (`run --review`) |
| ... osobne ujęcia | one more step: `python -m core.cli shots <folder> <opts>` (`run --shots`) |
| ... "Same ujęcia" | `analyze`, `plan`, `shots`: no `render` |
| ... jakość: Najlepsza / Dobra / Szybki podgląd | `--source sdk` / `--source ffmpeg` / `--source proxy`. "Najlepsza" needs the built stitcher (`core.stitch.find_exe`) and full-quality files; "Szybki podgląd" needs the camera's preview files |
| ... gdzie zapisać filmy | `--out <folder>` |
| ... więcej ustawień | `--profile <name>`, `--mount <name>`, `--length <s>`; a one-off profile is `--profile <output folder>\job_profile.yaml` |
| Przejrzyj ujęcia: Zapisz zmiany | edit `<output folder>\reel_plan_<tag>.json` and set `"edited": true` |
| Przejrzyj ujęcia: Renderuj | `python -m core.cli plan <folder> <opts>`, then `render` (and `shots`) |
| Filmy: nowa wersja o innej długości | `python -m core.cli plan <folder> <opts> --length <new>`, then `render`: no analysis |
| Filmy: obejrzyj, pobierz | the files `<output folder>\reel_*.mp4` and `shots_*\` |
| Filmy: spróbuj ponownie | the failed command again |
| Osoby: Odśwież z Immicha | `python -m core.cli faces-export --people <names> --out people.json --immich-url <address> --api-key-env IMMICH_API_KEY` |
| Ustawienia: miejsce na pliki robocze | `--work <folder>\job_<id>` on every step |
| Ustawienia: zaawansowane | edit `profiles/*.yaml`, `profiles/mounts/*.yaml`, `profiles/themes/*.yaml` |

`app.thumbs` writes `<output folder>\review\<scene_id>.jpg` from the proxy with
`core.render.still`; the safe zone is drawn by the browser from `profile.safe_zone`.

### Immich

"Odśwież z Immicha" needs `IMMICH_API_KEY` and an address: `IMMICH_TAILSCALE_ADDRESS` is tried first
(a short connection test), then `IMMICH_ADDRESS`. They are read from the environment, then from the
`.env` file in the repository root. The key is handed to the command through its environment only:
it is never on a command line, in the log or in an API answer, and the page shows only whether each
value is set. The names to export are remembered in `settings.json`; the first time they are the
`people` of `profiles/holiday.yaml`.

## Code

| File | What |
|---|---|
| `__main__.py` | the command: port, single instance, browser, start and stop |
| `web.py` | FastAPI: the API behind the UI, and the UI files in `static/` |
| `people.py` | the "Osoby" API: `people.json`, the Immich settings, the export as a background action |
| `db.py` | the queue in SQLite |
| `runner.py` | the job runner thread and the subprocess runner (process-tree kill, Windows job object) |
| `steps.py` | job options -> CLI arguments |
| `thumbs.py` | the thumbnails step of the scene review |
| `static/` | `index.html` (the shell), `style.css` (all colours as custom properties), `app.js` (sidebar, routing), `live.js` (job polling, theme, player), `ui.js` (helpers, icons, the words for things), one module per screen |

## API in short

`GET /api/folders?path=` (no path: the drives and recent folders), `GET /api/folders/inspect?path=`
(recordings, total length, kinds of files, flight or holiday), `GET /api/meta`, `GET /api/status`,
`GET /api/films`, `GET|POST /api/jobs`, `GET|DELETE /api/jobs/{id}`,
`POST /api/jobs/{id}/cancel|retry|render`, `POST /api/jobs/{id}/version {length}`,
`GET /api/jobs/{id}/results`, `GET /api/jobs/{id}/timeline` (a flight's altitude in about 300 points
with the chosen shots, or a holiday's days), `GET /api/jobs/{id}/files/<path>[?download=1]` (HTTP range),
`GET /api/jobs/{id}/zip/shots_<tag>`, `GET|PUT /api/jobs/{id}/review`, `GET /api/people`,
`POST /api/people/refresh {names}`, `GET|PUT /api/config/{profiles|mounts|themes}/{name}`,
`POST /api/config/{kind}/validate`, `POST /api/config/compose`, `GET|PUT /api/settings`.
Interactive docs: `/api/docs`.
## Tests

```
python -m pytest tests/test_server.py -q
```
