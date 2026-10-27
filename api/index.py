"""
Vercel Python serverless entry: FastAPI puzzle matcher.
"""
from __future__ import annotations
import json
import sys
import time
from pathlib import Path
# Ensure project root is on path (puzzle_match.py sits next to api/)
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from puzzle_match import process_payload
app = FastAPI(title="Puzzle Match (Vercel)", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
INDEX_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Puzzle Match — Vercel bench</title>
  <style>
    :root {
      --bg: #0f1115;
      --card: #1a1d24;
      --text: #e6e8ee;
      --muted: #9aa3b2;
      --accent: #6ee7b7;
      --border: #2a2f3a;
      --err: #f87171;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
      background: var(--bg); color: var(--text); min-height: 100vh;
      display: flex; flex-direction: column; align-items: center; padding: 2rem 1rem;
    }
    h1 { font-size: 1.4rem; font-weight: 600; margin: 0 0 0.25rem; }
    .sub { color: var(--muted); font-size: 0.9rem; margin-bottom: 1.5rem; }
    .card {
      background: var(--card); border: 1px solid var(--border); border-radius: 12px;
      padding: 1.25rem 1.5rem; width: 100%; max-width: 720px;
    }
    label.file {
      display: flex; flex-direction: column; align-items: center; justify-content: center;
      border: 1px dashed var(--border); border-radius: 10px; padding: 2rem 1rem;
      cursor: pointer; transition: border-color .15s, background .15s;
    }
    label.file:hover { border-color: var(--accent); background: rgba(110,231,183,.04); }
    input[type=file] { display: none; }
    .btn {
      margin-top: 1rem; background: var(--accent); color: #0b1220; border: none;
      padding: 0.65rem 1.25rem; border-radius: 8px; font-weight: 600; cursor: pointer;
      font-size: 0.95rem;
    }
    .btn:disabled { opacity: .5; cursor: not-allowed; }
    #status { margin-top: 1rem; color: var(--muted); font-size: 0.9rem; min-height: 1.2em; }
    #status.err { color: var(--err); }
    pre {
      background: #0c0e12; border: 1px solid var(--border); border-radius: 8px;
      padding: 1rem; overflow: auto; font-size: 0.78rem; line-height: 1.45;
      max-height: 60vh; margin-top: 1rem;
    }
    .meta { display: flex; flex-wrap: wrap; gap: 0.75rem 1.25rem; margin-top: 1rem; font-size: 0.9rem; }
    .meta span { color: var(--muted); }
    .meta strong { color: var(--accent); font-weight: 600; }
  </style>
</head>
<body>
  <h1>Puzzle Match — Vercel</h1>
  <p class="sub">Загрузи JSON (bg + pieces). Время считается с момента старта обработки.</p>
  <div class="card">
    <form id="f">
      <label class="file">
        <span id="fname">Выбери JSON-файл</span>
        <input type="file" id="file" accept=".json,.txt,application/json,text/plain" />
      </label>
      <button class="btn" type="submit" id="go" disabled>Загрузить и обработать</button>
    </form>
    <div id="status"></div>
    <div class="meta" id="meta" style="display:none"></div>
    <pre id="out" style="display:none"></pre>
  </div>
  <script>
    const file = document.getElementById('file');
    const go = document.getElementById('go');
    const status = document.getElementById('status');
    const out = document.getElementById('out');
    const meta = document.getElementById('meta');
    const fname = document.getElementById('fname');
    file.addEventListener('change', () => {
      go.disabled = !file.files.length;
      fname.textContent = file.files.length ? file.files[0].name : 'Выбери JSON-файл';
      status.textContent = '';
      status.className = '';
      out.style.display = 'none';
      meta.style.display = 'none';
    });
    document.getElementById('f').addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!file.files.length) return;
      go.disabled = true;
      status.textContent = 'Загрузка и обработка…';
      status.className = '';
      out.style.display = 'none';
      meta.style.display = 'none';
      const fd = new FormData();
      fd.append('file', file.files[0]);
      const t0 = performance.now();
      try {
        const res = await fetch('/api/match', { method: 'POST', body: fd });
        const wallClient = performance.now() - t0;
        const data = await res.json();
        if (!res.ok) {
          status.textContent = data.detail || data.error || ('HTTP ' + res.status);
          status.className = 'err';
          go.disabled = false;
          return;
        }
        status.textContent = 'Готово';
        meta.style.display = 'flex';
        meta.innerHTML =
          '<span>wall (server): <strong>' + (data.wall_ms ?? '—') + ' ms</strong></span>' +
          '<span>client RTT: <strong>' + wallClient.toFixed(1) + ' ms</strong></span>' +
          '<span>ok: <strong>' + (data.ok ?? 0) + '</strong></span>' +
          '<span>avg: <strong>' + (data.avg_timing_ms ?? '—') + ' ms</strong></span>';
        out.style.display = 'block';
        out.textContent = JSON.stringify(data, null, 2);
      } catch (err) {
        status.textContent = String(err);
        status.className = 'err';
      }
      go.disabled = false;
    });
  </script>
</body>
</html>
"""
@app.get("/", response_class=HTMLResponse)
async def index():
    return INDEX_HTML
@app.get("/api/health")
async def health():
    return {"ok": True, "service": "puzzle-match"}
@app.post("/api/match")
async def match(request: Request):
    """Accept either a multipart JSON file or a JSON request body; process sequentially."""
    t_start = time.perf_counter()
    filename = None
    content_type = request.headers.get("content-type", "").lower()

    try:
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            uploaded = form.get("file")
            if uploaded is None or not hasattr(uploaded, "read"):
                raise HTTPException(
                    status_code=400,
                    detail="Multipart request must include a file field named 'file'",
                )
            filename = getattr(uploaded, "filename", None)
            raw = await uploaded.read()
        else:
            raw = await request.body()

        text = raw.decode("utf-8-sig")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot read request body: {e}")

    # Accept ordinary JSON and JSON embedded after leading diagnostic text.
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        start_arr = text.find("[")
        start_obj = text.find("{")
        data = None
        if start_arr >= 0 and (start_obj < 0 or start_arr < start_obj):
            try:
                data, _ = decoder.raw_decode(text, start_arr)
            except (json.JSONDecodeError, ValueError):
                pass
        if data is None and start_obj >= 0:
            try:
                data, _ = decoder.raw_decode(text, start_obj)
            except (json.JSONDecodeError, ValueError):
                pass
        if data is None:
            raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        # Keep the original sequential matcher; do not dispatch jobs to workers.
        report = process_payload(data, step_coarse=4, step_fine=2)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not isinstance(report, dict):
        raise HTTPException(
            status_code=500,
            detail="Matcher returned an unexpected response type",
        )
    if filename is not None:
        report["filename"] = filename
    report["server_wall_from_read_ms"] = round(
        (time.perf_counter() - t_start) * 1000.0,
        3,
    )
    return JSONResponse(report)
# Vercel Python runtime expects a module-level ASGI application.
handler = app
