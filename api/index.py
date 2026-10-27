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

from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from puzzle_match import process_payload, load_json

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
async def match(file: UploadFile = File(...)):
    """Accept JSON file (same format as CLI), process, return results + profile."""
    t_start = time.perf_counter()
    try:
        raw = await file.read()
        text = raw.decode("utf-8-sig")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Cannot read file: {e}")

    # Parse JSON (robust, same as load_json logic)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        start_arr, start_obj = text.find("["), text.find("{")
        data = None
        if start_arr >= 0 and (start_obj < 0 or start_arr < start_obj):
            try:
                data, _ = decoder.raw_decode(text, start_arr)
            except Exception:
                pass
        if data is None and start_obj >= 0:
            try:
                data, _ = decoder.raw_decode(text, start_obj)
            except Exception:
                pass
        if data is None:
            raise HTTPException(status_code=400, detail="Invalid JSON")

    try:
        report = process_payload(data, step_coarse=4, step_fine=2)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    report["filename"] = file.filename
    report["server_wall_from_read_ms"] = round((time.perf_counter() - t_start) * 1000.0, 3)
    return JSONResponse(report)


# Vercel Python runtime expects a module-level handler for ASGI
handler = app
