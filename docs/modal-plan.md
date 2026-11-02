# Modal plan

Status (Mon Nov 2): a plan only. Nothing is deployed, and none of the code below has run. Prices and features come from Modal's pricing page and docs, read Nov 2, 2026 (sources at the end). Numbers marked "est." are worked out from those prices and from this repo's own measurements. They are not measured on Modal.

The budget is $100 of hackathon credits. The Starter plan also includes $30 of compute a month; whether the two stack wasn't checked.

## What to use it for

| Rank | Use | When | Modal pieces | Cost |
|---|---|---|---|---|
| 1 | **Host the app.** An always-on HTTPS server, so pings go out while the laptop is shut and the phone keeps one URL | Before the Wed Nov 4 deadline, kept up through judging (Nov 4–8) | A Server on CPU, a Volume | est. $0.40–0.76 a day |
| 2 | **Glimmer on a rented GPU for the System One eval.** Turns compare.py's "Modal A10 as a rental proxy" into a measured cost and frees the 3090 | When System One numbers are needed | A GPU Server (A10), a Volume for the GGUF | est. $3–3.50 per calibrate + compare cycle |
| 3 | **The decider on Modal for Part B** | After judging | The same GPU Server, with the decider in the container | $2.17 per 1,000 items at A10 prices (`eval/summary.md`), plus idle time |

Not worth it:

- **Hosting Spark.** muse-spark-1.3 is API-only. The open variant, `meta-models/Muse-Spark-Open-816B-A42B`, is 816B parameters, roughly 460 GB at 4 bits (est.), so it needs 3–4 B200s at $6.25 an hour each: about $25 an hour, or 4 hours for the whole $100. The API costs about 7 cents a prep, and the hackathon is built around the Meta API.
- **Modal Cron for the Canvas poll.** The app's background loop already polls in-process (`main.py`, `loop.py`).
- **Sandboxes for a code-checked second solve** (running sympy on practice answers). It would be a new feature. Park it until after the hackathon.

## The freeze

The MVP froze Sun Nov 1. Use 1 deploys the frozen code with no app changes. It is the Thu Oct 29 target "HTTPS deploy", done so it no longer depends on the laptop. Use 2 changes only `decider/`, `glimmer_check.py` and `eval/`, which aren't wired into the app. Use 3 is new work, so it waits until after judging.

## Use 1: the app on Modal

Today, pings only go out while the laptop is on, and the quick tunnel (`cloudflared tunnel --url ...`) gets a new URL on every restart. A new URL means a new origin, so the home-screen PWA and its push subscription have to be set up again each time. The indie track is judged on "whether it works", and judging runs Nov 4–8.

```
 now                                          on Modal
 ───                                          ────────
 phone ──push── laptop (uvicorn + loop)       phone ──push──┐
   │              │  SQLite, data/files         │           │
   └── random trycloudflare URL ──┘             └── https://<workspace>--<hard-to-guess app>-...modal.run
       (changes on restart, off when                       │
        the laptop sleeps)                     ┌──────────▼─────────────────────────┐
                                               │ 1 container, always on (CPU)        │
                                               │  uvicorn app.main:app  (unchanged)  │
                                               │  SQLite on local disk ──backup 60s──┼──> Volume: app.db
                                               │  FILES_DIR ─────────────────────────┼──> Volume: files/
                                               └────────────────────────────────────┘
                                                    │ Canvas, Meta API, web push (as today)
```

Design decisions:

- **One container, always on** (`min_containers=1`, `max_containers=1`). The start loop is an asyncio task inside the app (`main.py` lifespan), so the process has to stay up. There must never be two at once: two loops mean double pings and two copies of the DB.
- **`@app.server`, not `@modal.web_server` or `@modal.asgi_app`.** Web functions cut a request off at 150 s and answer with a 303 redirect. `/api/sync` and `/api/tests/upload` wait until every card or every page is done, so a slow one could break on camera. Servers have no such cap ("Request timeouts cannot be customized within Modal and must be set by the client or server code"). Test a long sync anyway.
- **Public, so the URL is the only lock.** Browsers can't send Modal proxy tokens, so the Server needs `unauthenticated=True`. The app has no login: anyone with the URL sees the cards and grades and can spend model credit, the same as with the tunnel today. Server URLs are built from the workspace, app and class names, with no random part. Put a random word in the app name, set it from an environment variable at deploy time, and keep it out of this public repo.
- **The live SQLite DB stays on the container's own disk.** The DB runs in WAL mode (`db.py`). Volumes have no file locking, and concurrent commits are last-write-wins, so the live file doesn't go on the Volume. The wrapper copies it there with SQLite's backup API every 60 s and on shutdown, and restores it at start. After a hard kill, the worst case is losing the last minute of writes.
- **Attachments and photos go straight to the Volume** (`FILES_DIR`). They're write-once files, which is what Volumes are built for. Their paths are worked out from `FILES_DIR` at run time and never stored in the DB, so moving the folder breaks nothing.
- **Preemption.** Any container can be preempted ("rare"). The exit handler runs in a grace period, and the final backup happens there. `nonpreemptible=True` triples the CPU and memory price. It's worth it for Nov 4–8 only.
- **`TIMEZONE` must be set.** When it's blank, the app uses the machine's zone, and the container's is probably UTC, which would put every block 7–8 hours off.
- **Secrets.** The `.env` goes in through `modal.Secret.from_dotenv()`, followed by a `Secret.from_dict` that overrides `DB_PATH` and `FILES_DIR` and sets `TIMEZONE` (later secrets win). The image gets only `app/`, `static/` and the dependencies from `pyproject.toml`, never `.env` or `data/`.
- **LibreOffice in the image**, so DOCX/PPTX conversion works there (`convert.py` finds `soffice` on PATH).

Steps:

1. `pip install modal`, then `modal setup`. Set a workspace budget of $100 in Modal's settings.
2. Write `modal_app.py` (sketch below). Add `modal = ["modal"]` to the optional dependencies in `pyproject.toml`.
3. Stop the laptop server. Make a single-file copy of the DB, with the WAL merged: `python -c "import sqlite3; sqlite3.connect('data/app.db').backup(sqlite3.connect('data/upload.db'))"`. Upload it and the files: `modal volume put start-helper-data data/upload.db /app.db` and `modal volume put start-helper-data data/files /files`.
4. `modal deploy modal_app.py`. Open the URL and check the cards, the clock bar and `/docs`.
5. Phone: open the new URL in Chrome, add it to the home screen, press *Turn on notifications*. Under Dashboard → *Notifications and extension*, remove the old tunnel devices, since their pings would open the dead tunnel URL. Keep the same VAPID keys.
6. Extension: in Options, set the server to the Modal URL. It already asks for the https permission (`extension/lib.js`).
7. Test with the PWA closed and the screen off: a real block ping, a check-in, a +15 min reflow. Then a manual Sync that generates cards, to try the long-request path.
8. Never run the laptop server against the same Canvas account at the same time. To redeploy, run `modal app stop <name>` and then `modal deploy`, so the old container's final backup lands before the new one restores it.

Cost: requesting 0.5 core and 1 GiB is $0.0236 + $0.0080 = $0.032 an hour. That's $0.76 a day, or about $23 for 30 days. Modal bills the higher of the request and actual use, so a mostly idle app pays for its request. Requesting 0.25 core and 512 MiB halves it. Non-preemptible at 0.5 core: $2.27 a day.

Sketch (untested):

```python
"""Start Helper on Modal: one always-on container running the laptop's uvicorn command (docs/modal-plan.md)."""

import os
import shutil
import sqlite3
import subprocess
import threading
import time

import modal

NAME = os.environ.get("START_HELPER_APP", "start-helper")  # part of the URL: make it hard to guess, don't commit it
LOCAL_DB = "/root/db/app.db"  # the live DB (WAL) on the container's own disk
VOL = "/vol"
BACKUP = f"{VOL}/app.db"

volume = modal.Volume.from_name("start-helper-data", create_if_missing=True)
image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("libreoffice-writer", "libreoffice-impress", "libreoffice-calc")
    .pip_install_from_pyproject("pyproject.toml")
    .add_local_dir("app", "/root/app")
    .add_local_dir("static", "/root/static")
)
app = modal.App(NAME, image=image)
secrets = [
    modal.Secret.from_dotenv(),  # the laptop's .env
    modal.Secret.from_dict({  # later secrets win
        "DB_PATH": LOCAL_DB,
        "FILES_DIR": f"{VOL}/files",
        "TIMEZONE": "America/Los_Angeles",
    }),
]


def backup() -> None:
    src, dst = sqlite3.connect(LOCAL_DB), sqlite3.connect(BACKUP + ".tmp")
    src.backup(dst)
    dst.close()
    src.close()
    os.replace(BACKUP + ".tmp", BACKUP)
    volume.commit()


@app.server(
    port=8000, unauthenticated=True, min_containers=1, max_containers=1,
    cpu=0.5, memory=1024, volumes={VOL: volume}, secrets=secrets, startup_timeout=120,
    nonpreemptible=False,  # True for Nov 4-8: 3x the CPU and memory price
)
class StartHelper:
    @modal.enter()
    def start(self):
        os.makedirs(os.path.dirname(LOCAL_DB), exist_ok=True)
        if os.path.exists(BACKUP):
            shutil.copy(BACKUP, LOCAL_DB)
        self.proc = subprocess.Popen(
            ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"], cwd="/root"
        )
        threading.Thread(target=self._every_minute, daemon=True).start()

    def _every_minute(self):
        while True:
            time.sleep(60)
            try:
                backup()
            except Exception as e:  # keep serving; the next minute tries again
                print("backup failed:", e)

    @modal.exit()
    def stop(self):
        self.proc.terminate()
        self.proc.wait(timeout=20)
        backup()
```

### 1b (optional): a public demo copy for judges

This is a second app from the same file, with fabricated data (`python -m app.demo` against its own DB) and `CANVAS_TOKEN` blank. Either leave out `META_API_KEY`, or accept that anyone with the link can spend it: a test upload calls Spark. It can scale to zero (`min_containers=0`, `scaledown_window=1200`) because it only needs to be awake while someone is looking, and jumping the simulated clock fires pings on request. Cost: cents a day. Every visitor shares one DB and one simulated clock.

## Use 2: Glimmer on Modal for the eval

`eval/compare.py` prices Glimmer at "Modal's A10, as a rental proxy" ($1.10 an hour; Modal's list price is $0.000306 a second, which is the same). Running on an actual A10 turns that proxy into a measurement. It also frees the 3090, where the ninfer Qwen server and Glimmer don't fit together (spec, Serving), and lets two setups run on two GPUs at once.

Glimmer has open weights (`meta-models/Muse-Glimmer-30B` and `-GGUF` on Hugging Face, Apache-2.0, 29.8B parameters). The GGUF downloads from Hugging Face into a Volume inside Modal, so there's no 17 GB upload from home. Use the same file the 3090 runs use (`Muse-Glimmer-30B-KQuant-17GB-Q4_K_M`), or the numbers won't compare.

Design:

- **Image:** the official llama.cpp CUDA server image, pinned to b12023, the build glimmer_check passed on. The GitHub registry lists server images around b12019–b12033. Confirm the `server-cuda-b12023` tag exists, or take the nearest build at or above b10353.
- **`@app.server(gpu="A10", port=8080, max_containers=1, min_containers=0, scaledown_window=300, startup_timeout=600)`.** Only one container, because `id_slot` pinning and the prompt cache live inside one llama-server process. Proxy auth stays on (the default for Servers), so the spec's rule "never expose llama-server outside the machine" still holds in spirit.
- **Command:** the spec's Serving line, with `--host 0.0.0.0`.
- **Client:** send the `Modal-Key` and `Modal-Secret` headers. Give `decider.llama.Llama` an optional `headers` argument, add `GLIMMER_MODAL_KEY` / `GLIMMER_MODAL_SECRET` to `decider/config.py`, and pass them at the three call sites (`decider/main.py`, `decider/live_check.py`, `eval/run.py`). Do the same in `glimmer_check.py` (requests), and for the d1 client in `eval/compare.py` if d1 moves too. Add a test in `tests/test_decider.py` against the fake server.

GPU choice. Decode speeds are est., scaled from the 34 tok/s measured on the 3090 by spec-sheet memory bandwidth, since decode is bandwidth-bound. llama.cpp usually scales worse than that on the big cards. Measure with glimmer_check check 5 before trusting any row.

| GPU | VRAM | $/hour | Bandwidth | Decode (est.) | Fit for this |
|---|---|---|---|---|---|
| L4 | 24 GB | 0.80 | 300 GB/s | ~11 tok/s | Cheapest that fits; slow thinking loops |
| **A10** | 24 GB | 1.10 | 600 GB/s | ~22 tok/s | Same VRAM class as the 3090 and the eval's proxy price. With ECC on, usable memory may be a little below the 3090's: if `-c 32768 -np 4` doesn't fit, try `-np 3` or an L40S |
| L40S | 48 GB | 1.95 | 864 GB/s | ~31 tok/s | Close to the 3090; room for `-np 8` |
| A100 40 GB | 40 GB | 2.10 | 1,555 GB/s | ~50 tok/s | Faster eval runs |
| H100 | 80 GB | 3.95 | 3,350 GB/s | ~80–120 tok/s | Only if wall clock matters more than cost |

Steps:

1. Write `modal_glimmer.py` (sketch below).
2. `modal run modal_glimmer.py::download --repo <the repo the GGUF came from>`.
3. `modal deploy modal_glimmer.py`, then `GLIMMER_URL=<url> python glimmer_check.py`. All 5 checks must pass, in particular check 4 (cache) and check 5 (speed).
4. Correctness run from the laptop: `python -m eval.run compare --limit 20`.
5. Timings to publish: every probe is an HTTP round trip, so a run from home adds network time to "seconds per item". Either run the harness inside a Modal container next to llama-server, with the eval data on a private Volume (it's real assignment text), or report the timings with that caveat.

Cost: on the 3090, one calibrate (723 fields at 2.13 s, about 26 min) plus compare's three Glimmer setups (248 items at 9.13 + 7.10 + 8.21 s, about 101 min) take about 2.1 hours. On an A10, figure 2.5–3 hours, about $3–3.50 a cycle (est.). Spark's arm of compare is billed to Meta credits, as now ($2.45 a run).

**Guardrail:** never set `min_containers` to 1 or more on the GPU Server. An idle warm A10 costs $26.40 a day, which would eat the $100 in under four days.

Sketch (untested):

```python
"""Muse Glimmer's llama-server on a Modal GPU, behind Modal proxy auth (docs/modal-plan.md)."""

import subprocess
import time
import urllib.request

import modal

LLAMA_IMAGE = "ghcr.io/ggml-org/llama.cpp:server-cuda-b12023"  # check the tag; >= b10353 knows muse-glimmer
GGUF = "Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf"  # the exact file the 3090 runs use
PORT = 8080

models = modal.Volume.from_name("glimmer-models", create_if_missing=True)
image = modal.Image.from_registry(LLAMA_IMAGE, add_python="3.12").entrypoint([]).pip_install("huggingface_hub")
app = modal.App("glimmer", image=image)


@app.function(volumes={"/models": models}, timeout=3600)
def download(repo: str, filename: str = GGUF):
    from huggingface_hub import hf_hub_download

    hf_hub_download(repo, filename, local_dir="/models")
    models.commit()


@app.server(
    gpu="A10", port=PORT, volumes={"/models": models},
    min_containers=0, max_containers=1,  # never min >= 1: an idle A10 is $26/day
    scaledown_window=300, startup_timeout=600,
)
class Glimmer:
    @modal.enter()
    def start(self):
        self.proc = subprocess.Popen([
            "/app/llama-server", "-m", f"/models/{GGUF}", "-ngl", "99", "-c", "32768", "-np", "4",
            "--swa-full", "--jinja", "--host", "0.0.0.0", "--port", str(PORT),
        ])
        while True:  # llama-server answers /health with 503 while the model loads
            if self.proc.poll() is not None:
                raise RuntimeError("llama-server exited")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=5)
                return
            except Exception:
                time.sleep(2)

    @modal.exit()
    def stop(self):
        self.proc.terminate()
```

## Use 3: the decider on Modal (Part B, after judging)

The spec already plans for this: "Where the decider runs is just config (`DECIDER_URL`), so it can move to a VPS or Modal later without code changes." Run the decider (`python -m decider`) and llama-server in the same GPU container. That way the many probes stay inside the container, and only one HTTP call per assignment (or per test) crosses the network, as the spec's architecture wants. Expose only the decider, with proxy auth, and have the app on Modal send the token. The GPU scales to zero, and Part B's fallback (the `rule` tier and the re-decide list) covers cold starts and outages.

Cost: $2.17 per 1,000 items at A10 prices (`eval/summary.md`, System One full), plus about $0.09 for each 5-minute idle tail after a burst. Cold start hasn't been measured: it's loading 17 GB from a Volume plus starting CUDA. Measure it before deciding whether GPU memory snapshots are worth trying.

## Budget

| Item | Through Nov 8 (est.) |
|---|---|
| App, preemptible Nov 2–4, non-preemptible Nov 4–8 | ~$11 (all preemptible: ~$5) |
| Demo copy, scale to zero | < $1 |
| Glimmer: download, glimmer_check, 1–2 eval cycles | ~$5–8 |
| **Total** | **~$10–20 of the $100** |

What's left (about $80–90) covers a month of app hosting ($11–23) and Part B testing. Volume storage is free up to 1 TiB a month, so the 17 GB GGUF costs nothing to keep.

## Open questions

- Is System One in the submission (the eval numbers), or only after it? That decides whether Use 2 happens before Wednesday.
- A public demo link for judges (1b): yes or no, and with or without a model key?
- Real data on Modal: the hosted app holds real Canvas data and graded-test photos in a private Volume. They already go to Meta's API (standard tier); this adds Modal as somewhere they're stored.

## Sources

- Pricing, plans and billing rules: https://modal.com/pricing
- Servers (`@app.server`, default auth, no request-timeout cap): https://modal.com/docs/guide/servers
- `App.server` parameters: https://modal.com/docs/sdk/py/latest/App
- Web function 150 s limit and 303 redirect: https://modal.com/docs/guide/webhook-timeouts
- Volumes (commits, no file locking, last write wins): https://modal.com/docs/guide/volumes
- Preemption and `nonpreemptible`: https://modal.com/docs/guide/preemption
- Cold starts, `min_containers`, `scaledown_window`: https://modal.com/docs/guide/cold-start
- Web function URLs and labels: https://modal.com/docs/guide/webhook-urls
- Proxy tokens (`Modal-Key`, `Modal-Secret`): https://modal.com/docs/guide/webhook-proxy-auth
- Secrets (`from_dotenv`, `from_dict`, order): https://modal.com/docs/guide/secrets
- Modal's llama.cpp Server example: https://modal.com/docs/examples/liquidai_embeddings_server
- llama.cpp container images: https://github.com/ggml-org/llama.cpp/pkgs/container/llama.cpp
- Glimmer weights: https://hf.co/meta-models/Muse-Glimmer-30B-GGUF
- Spark open weights: https://hf.co/meta-models/Muse-Spark-Open-816B-A42B
- Hackathon dates and indie-track judging: https://cerebralvalley.ai/e/meta-ai-developer-hackathon
