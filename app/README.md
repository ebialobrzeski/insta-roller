# insta-roller: the application (stage 7)

One program on the Windows PC: a job queue, a job runner and a browser UI (Polish, plain
HTML + JS). The UI adds nothing of its own to the pipeline: every job is a list of ordinary
CLI steps (`python -m core.cli analyze|plan|render|shots`), run one after another as
subprocesses, and the queue screen shows the exact commands.

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

- **Job folder**: any folder on the machine. The picker on "Nowe zadanie" starts at the drives,
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

## What the UI does, as CLI commands

`<folder>` is the job folder, `<opts>` the job's options.

| UI | Commands |
|---|---|
| Nowe zadanie | `python -m core.cli analyze <folder> <opts>`, then `plan`, then `render` (same as `run`) |
| ... folder wyników | `--out <folder>` |
| ... profil | `--profile auto\|flight\|flight_motor\|holiday\|<name>` |
| ... profil "inne" | `--profile <output folder>\job_profile.yaml` (the YAML from the form) |
| ... montaż kamery | `--mount <name>` |
| ... długość | `--length <s>` |
| ... tematy (checkboxes) | `--enable landing,...` / `--disable thermal,...` (only differences from the theme files) |
| ... tryb dzień / wyjazd / oba | `--mode day\|trip\|both` |
| ... osoby | `--people Ala,Olek` |
| ... nakładka | `--overlay` / `--no-overlay` |
| ... źródło obrazu | `--source proxy\|sdk` |
| ... eksport ujęć | one more step: `python -m core.cli shots <folder> <opts>` (`run --shots`) |
| ... przegląd scen | `analyze`, `plan`, then `python -m app.thumbs <folder> <opts>`; the job waits (`run --review`) |
| Przegląd scen: Zapisz | edit `<output folder>\reel_plan_<tag>.json` and set `"edited": true` |
| Przegląd scen: Renderuj | `python -m core.cli plan <folder> <opts>`, then `render` (and `shots`) |
| Wyniki: nowa wersja | `python -m core.cli plan <folder> <opts> --length <new>`, then `render`: no analysis |
| Wyniki: podgląd, pobranie | the files `<output folder>\reel_*.mp4` and `shots_*\` |
| Ustawienia; zapisz jako nowy profil | edit `profiles/*.yaml`, `profiles/mounts/*.yaml`, `profiles/themes/*.yaml` |
| Ustawienia: folder pamięci podręcznej | `--work <folder>\job_<id>` on every step |
| Kolejka: ponów | the failed command again |
| lista profili | `python -m core.cli profiles` |

`app.thumbs` writes `<output folder>\review\<scene_id>.jpg` from the proxy with
`core.render.still`; the safe zone is drawn by the browser from `profile.safe_zone`.
The names offered under "Osoby" come from the repository's `people.json` and the profiles.

## Code

| File | What |
|---|---|
| `__main__.py` | the command: port, single instance, browser, start and stop |
| `web.py` | FastAPI: the API behind the UI, and the UI files in `static/` |
| `db.py` | the queue in SQLite |
| `runner.py` | the job runner thread and the subprocess runner (process-tree kill, Windows job object) |
| `steps.py` | job options -> CLI arguments |
| `thumbs.py` | the thumbnails step of the scene review |

## API in short

`GET /api/folders?path=` (no path: the drives), `GET /api/meta`, `GET|POST /api/jobs`,
`GET /api/jobs/{id}`, `POST /api/jobs/{id}/cancel|retry|render`, `POST /api/jobs/{id}/version {length}`,
`GET /api/jobs/{id}/results`, `GET /api/jobs/{id}/files/<path>[?download=1]` (HTTP range),
`GET /api/jobs/{id}/zip/shots_<tag>`, `GET|PUT /api/jobs/{id}/review`,
`GET|PUT /api/config/{profiles|mounts|themes}/{name}`, `POST /api/config/{kind}/validate`,
`POST /api/config/compose`, `GET|PUT /api/settings`. Interactive docs: `/api/docs`.

## Tests

```
python -m pytest tests/test_server.py -q
```
