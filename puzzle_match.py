#!/usr/bin/env python3
"""
FastAPI service for puzzle captcha matcher.

Endpoints:
  POST /match          — JSON body (object or array), same format as file
  POST /match/file     — upload JSON file (multipart)
  GET  /health         — health check

Concurrency: up to MAX_WORKERS (default 3) parallel CPU-bound matches
via ThreadPoolExecutor. Batch items are processed concurrently.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from typing import Any, Optional, Union

from fastapi import Body, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:
    from PIL import Image, ImageFilter
except ImportError:
    raise ImportError("Need Pillow")

try:
    import numpy as np
except ImportError:
    raise ImportError("Need numpy")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

MAX_WORKERS = 3  # parallel puzzle matches (2–3 as requested)
STEP_COARSE = 4
STEP_FINE = 2

app = FastAPI(
    title="Puzzle Captcha Matcher",
    version="1.1.0",
    description="Match puzzle pieces to background slots. Accepts JSON body or file upload.",
)

_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)


# ---------------------------------------------------------------------------
# I/O (from original)
# ---------------------------------------------------------------------------

def load_json_bytes(raw: bytes | str):
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8-sig")
    try:
        data = json.loads(raw)
        if isinstance(data, (dict, list)):
            return data
        raise ValueError("JSON root must be object or array")
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    start_arr, start_obj = raw.find("["), raw.find("{")
    if start_arr >= 0 and (start_obj < 0 or start_arr < start_obj):
        data, _ = decoder.raw_decode(raw, start_arr)
        if isinstance(data, list):
            return data
    if start_obj < 0:
        raise ValueError("JSON not found")
    data, _ = decoder.raw_decode(raw, start_obj)
    if not isinstance(data, dict):
        raise ValueError("JSON root is not object")
    return data


def decode_base64(value: str) -> bytes:
    value = value.strip()
    if value.startswith("data:") and "," in value:
        value = value.split(",", 1)[1]
    value = re.sub(r"\s+", "", value)
    value += "=" * (-len(value) % 4)
    return base64.b64decode(value, validate=True)


def load_rgba_b64(value: str) -> Image.Image:
    return Image.open(BytesIO(decode_base64(value))).convert("RGBA")


def collect_piece_b64(data: dict) -> list[str]:
    pieces = data.get("pieces")
    if isinstance(pieces, list) and pieces:
        out = []
        for p in pieces:
            if isinstance(p, dict):
                p = next(
                    (
                        p[k]
                        for k in ("base64", "data", "image", "png")
                        if isinstance(p.get(k), str)
                    ),
                    None,
                )
            if not isinstance(p, str):
                raise ValueError("Invalid pieces element")
            out.append(p)
        return out
    found = []
    for key, val in data.items():
        if re.fullmatch(r"piece[_-]?\d+", str(key).lower()):
            if isinstance(val, dict):
                val = next(
                    (
                        val[k]
                        for k in ("base64", "data", "image", "png")
                        if isinstance(val.get(k), str)
                    ),
                    None,
                )
            found.append((str(key), val))
    if not found:
        raise ValueError("pieces not found")
    found.sort(key=lambda kv: kv[0])
    return [v for _, v in found]


# ---------------------------------------------------------------------------
# Masks & shape
# ---------------------------------------------------------------------------

def alpha_mask_from_rgba(rgba: np.ndarray, thr: int = 10) -> np.ndarray:
    return rgba[:, :, 3] > thr


def boundary_coords(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    m = mask.astype(np.uint8)
    p = np.pad(m, 1)
    c = p[1:-1, 1:-1]
    b = (c == 1) & (
        (p[:-2, 1:-1] == 0)
        | (p[2:, 1:-1] == 0)
        | (p[1:-1, :-2] == 0)
        | (p[1:-1, 2:] == 0)
    )
    return np.where(b)


def analyze_shape(mask: np.ndarray, body_fill: float = 0.55) -> dict:
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return {"spikes": [], "caves": [], "fill": 0.0}

    y0, y1 = int(ys.min()), int(ys.max())
    x0, x1 = int(xs.min()), int(xs.max())
    m = mask[y0 : y1 + 1, x0 : x1 + 1]
    mh, mw = m.shape
    if mh < 4 or mw < 4:
        return {"spikes": [], "caves": [], "fill": float(m.mean())}

    col_fill = m.sum(axis=0) / max(mh, 1)
    row_fill = m.sum(axis=1) / max(mw, 1)
    main_cols = np.where(col_fill > body_fill)[0]
    main_rows = np.where(row_fill > body_fill)[0]
    if len(main_cols) == 0 or len(main_rows) == 0:
        return {"spikes": [], "caves": [], "fill": float(m.mean())}

    bx0, bx1 = int(main_cols[0]), int(main_cols[-1])
    by0, by1 = int(main_rows[0]), int(main_rows[-1])
    body_w = max(1, bx1 - bx0 + 1)
    body_h = max(1, by1 - by0 + 1)

    spikes: list[str] = []
    caves: list[str] = []

    top = m[: max(1, by0), bx0 : bx1 + 1]
    if top.size and top.mean() > 0.25:
        spikes.append("TOP")
    elif by0 > body_h * 0.12 and m[0:by0, bx0 : bx1 + 1].mean() < 0.2:
        caves.append("TOP")

    bot = m[min(mh, by1 + 1) :, bx0 : bx1 + 1]
    if bot.size and bot.mean() > 0.25:
        spikes.append("BOTTOM")
    elif (mh - 1 - by1) > body_h * 0.12 and m[by1 + 1 :, bx0 : bx1 + 1].mean() < 0.2:
        caves.append("BOTTOM")

    left = m[by0 : by1 + 1, : max(1, bx0)]
    if left.size and left.mean() > 0.25:
        spikes.append("LEFT")
    elif bx0 > body_w * 0.12 and m[by0 : by1 + 1, :bx0].mean() < 0.2:
        caves.append("LEFT")

    right = m[by0 : by1 + 1, min(mw, bx1 + 1) :]
    if right.size and right.mean() > 0.25:
        spikes.append("RIGHT")
    elif (mw - 1 - bx1) > body_w * 0.12 and m[by0 : by1 + 1, bx1 + 1 :].mean() < 0.2:
        caves.append("RIGHT")

    return {
        "spikes": spikes,
        "caves": caves,
        "fill": float(m.mean()),
    }


def shape_key(shape: dict) -> tuple:
    return (tuple(sorted(shape["spikes"])), tuple(sorted(shape["caves"])))


def normalized_mask(mask: np.ndarray, size: int = 32) -> np.ndarray:
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return np.zeros((size, size), dtype=np.float32)
    y0, y1 = int(ys.min()), int(ys.max())
    x0, x1 = int(xs.min()), int(xs.max())
    crop = mask[y0 : y1 + 1, x0 : x1 + 1].astype(np.float32)
    h, w = crop.shape
    yy = (np.arange(size) * h / size).astype(np.int32)
    xx = (np.arange(size) * w / size).astype(np.int32)
    yy = np.clip(yy, 0, h - 1)
    xx = np.clip(xx, 0, w - 1)
    return crop[yy][:, xx]


def mask_l1(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a - b).sum())


# ---------------------------------------------------------------------------
# Slot search
# ---------------------------------------------------------------------------

def edge_map(gray_u8: np.ndarray) -> np.ndarray:
    im = Image.fromarray(gray_u8, mode="L")
    return np.asarray(im.filter(ImageFilter.FIND_EDGES), dtype=np.float32)


def find_slot(
    bg_edges: np.ndarray,
    mask: np.ndarray,
    step_coarse: int = 8,
    step_fine: int = 2,
    top_k: int = 3,
) -> tuple[int, int, float]:
    ys, xs = boundary_coords(mask)
    if len(xs) == 0:
        return 0, 0, -1.0

    bh, bw = bg_edges.shape
    ph, pw = mask.shape
    max_y = max(1, bh - ph + 1)
    max_x = max(1, bw - pw + 1)

    peaks: list[tuple[float, int, int]] = []
    xgrid = np.arange(0, max_x, step_coarse, dtype=np.intp)
    for y in range(0, max_y, step_coarse):
        scores = bg_edges[y + ys[:, None], xgrid[None, :] + xs[:, None]].sum(axis=0)
        peaks.extend((float(score), int(x), y) for x, score in zip(xgrid, scores))
    peaks.sort(reverse=True, key=lambda t: t[0])
    peaks = peaks[: max(1, top_k)]

    best, bx, by = -1.0, 0, 0
    radius = max(step_coarse, 8)
    for _, cx, cy in peaks:
        y0 = max(0, cy - radius)
        y1 = min(max_y, cy + radius + 1)
        x0 = max(0, cx - radius)
        x1 = min(max_x, cx + radius + 1)
        xgrid = np.arange(x0, x1, step_fine, dtype=np.intp)
        for y in range(y0, y1, step_fine):
            scores = bg_edges[y + ys[:, None], xgrid[None, :] + xs[:, None]].sum(axis=0)
            for x, score in zip(xgrid, scores):
                score = float(score)
                if score > best:
                    best, bx, by = score, int(x), y
    return bx, by, best


# ---------------------------------------------------------------------------
# Seam fit
# ---------------------------------------------------------------------------

_OUTWARD = ((-2, 0), (2, 0), (0, -2), (0, 2), (-1, 0), (1, 0), (0, -1), (0, 1))


def seam_costs(
    bg_rgb: np.ndarray,
    piece_rgb: np.ndarray,
    mask: np.ndarray,
    slot_x: int,
    slot_y: int,
) -> tuple[float, float]:
    ys, xs = boundary_coords(mask)
    if len(xs) == 0:
        return 1e9, 1e9

    ph, pw = mask.shape
    bh, bw = bg_rgb.shape[:2]
    seam, bright = [], []

    for y, x in zip(ys.tolist(), xs.tolist()):
        out = None
        for dy, dx in _OUTWARD:
            ny, nx = y + dy, x + dx
            if 0 <= ny < ph and 0 <= nx < pw and not mask[ny, nx]:
                by, bx = slot_y + ny, slot_x + nx
                if 0 <= by < bh and 0 <= bx < bw:
                    out = (by, bx)
                    break
        if out is None:
            continue
        by, bx = out
        pi = piece_rgb[y, x].astype(np.float32)
        bi = bg_rgb[by, bx].astype(np.float32)
        seam.append(float(np.linalg.norm(pi - bi)))
        bright.append(abs(float(pi.mean() - bi.mean())))

    if not seam:
        return 1e9, 1e9
    return float(np.mean(seam)), float(np.mean(bright))


def _norm(vals: list[float]) -> list[float]:
    lo, hi = min(vals), max(vals)
    if hi - lo < 1e-9:
        return [0.0] * len(vals)
    return [(v - lo) / (hi - lo) for v in vals]


# ---------------------------------------------------------------------------
# Core match (original, sync — runs in thread pool)
# ---------------------------------------------------------------------------

def match_puzzle(data: dict, step_coarse: int = 4, step_fine: int = 2) -> dict:
    profile: dict[str, float] = {}
    t_all = time.perf_counter()

    bg_enc = data.get("bg") or data.get("background")
    if not bg_enc:
        raise ValueError("no bg field")

    t = time.perf_counter()
    piece_encs = collect_piece_b64(data)
    profile["collect_b64_refs"] = (time.perf_counter() - t) * 1000.0

    t = time.perf_counter()
    bg_img = load_rgba_b64(bg_enc)
    profile["decode_bg"] = (time.perf_counter() - t) * 1000.0

    t = time.perf_counter()
    bg_rgb = np.asarray(bg_img.convert("RGB"), dtype=np.uint8)
    bg_gray = np.asarray(bg_img.convert("L"), dtype=np.uint8)
    profile["bg_to_numpy"] = (time.perf_counter() - t) * 1000.0

    t = time.perf_counter()
    bg_edges = edge_map(bg_gray)
    profile["bg_edge_map"] = (time.perf_counter() - t) * 1000.0

    pieces_rgba = []
    masks = []
    rgbs = []
    shapes = []
    norms = []
    fills = []

    t_dec_p = t_mask = t_shape = t_norm = 0.0
    for enc in piece_encs:
        t = time.perf_counter()
        im = load_rgba_b64(enc)
        arr = np.asarray(im, dtype=np.uint8)
        t_dec_p += time.perf_counter() - t

        t = time.perf_counter()
        m = alpha_mask_from_rgba(arr)
        pieces_rgba.append(arr)
        masks.append(m)
        rgbs.append(arr[:, :, :3].copy())
        t_mask += time.perf_counter() - t

        t = time.perf_counter()
        sh = analyze_shape(m)
        shapes.append(sh)
        fills.append(sh["fill"])
        t_shape += time.perf_counter() - t

        t = time.perf_counter()
        norms.append(normalized_mask(m))
        t_norm += time.perf_counter() - t

    profile["decode_pieces"] = t_dec_p * 1000.0
    profile["masks"] = t_mask * 1000.0
    profile["shape_spikes_caves"] = t_shape * 1000.0
    profile["norm_masks_32"] = t_norm * 1000.0

    n = len(masks)
    if n == 0:
        raise ValueError("no pieces")

    keys = [shape_key(s) for s in shapes]

    best_edge = -1.0
    slot_x = slot_y = 0
    template_i = 0
    slot_per_piece: dict[str, float] = {}

    t_slot_all = time.perf_counter()
    for i in range(n):
        t = time.perf_counter()
        x, y, sc = find_slot(
            bg_edges, masks[i], step_coarse=step_coarse, step_fine=step_fine
        )
        slot_per_piece[f"piece_{i}"] = (time.perf_counter() - t) * 1000.0
        if sc > best_edge:
            best_edge, slot_x, slot_y, template_i = sc, x, y, i
    profile["slot_search_total"] = (time.perf_counter() - t_slot_all) * 1000.0
    profile["slot_per_piece"] = slot_per_piece

    edge_score = best_edge
    dominant_key = keys[template_i]

    t = time.perf_counter()
    t_norm_arr = norms[template_i]
    l1s = [mask_l1(norms[i], t_norm_arr) for i in range(n)]
    best_l1 = min(l1s)
    threshold = best_l1 * 1.15 + 1.0
    same = [i for i in range(n) if keys[i] == dominant_key]
    close = [i for i in range(n) if l1s[i] <= threshold]
    candidates = sorted(set(same) | set(close))
    if not candidates:
        candidates = [template_i]
    profile["shape_candidates"] = (time.perf_counter() - t) * 1000.0

    t = time.perf_counter()
    if len(candidates) == 1:
        best = candidates[0]
        reason = "unique_shape"
        seam_scores = {}
    else:
        raw_seam = []
        raw_bright = []
        for i in candidates:
            s, b = seam_costs(bg_rgb, rgbs[i], masks[i], slot_x, slot_y)
            raw_seam.append(s)
            raw_bright.append(b)
        sn = _norm(raw_seam)
        bn = _norm(raw_bright)
        combined = [0.7 * sn[k] + 0.3 * bn[k] for k in range(len(candidates))]
        best = candidates[int(np.argmin(combined))]
        reason = "shape_plus_seam"
        seam_scores = {candidates[k]: float(combined[k]) for k in range(len(candidates))}
    profile["seam_fit"] = (time.perf_counter() - t) * 1000.0

    elapsed_ms = (time.perf_counter() - t_all) * 1000.0
    profile["total"] = elapsed_ms

    profile_pct = {
        k: round(100.0 * v / elapsed_ms, 1)
        for k, v in profile.items()
        if isinstance(v, (int, float)) and k != "total" and k != "slot_per_piece"
    }

    return {
        "best_index_0based": best,
        "best_index_1based": best + 1,
        "reason": reason,
        "slot_x": slot_x,
        "slot_y": slot_y,
        "candidates": candidates,
        "shape_scores": {i: float(l1s[i]) for i in range(n)},
        "seam_scores": seam_scores,
        "template_index": template_i,
        "edge_score": float(edge_score),
        "timing_ms": round(elapsed_ms, 3),
        "profile_ms": {k: round(v, 3) if isinstance(v, float) else v for k, v in profile.items()},
        "profile_pct": profile_pct,
        "bg_size": [int(bg_rgb.shape[1]), int(bg_rgb.shape[0])],
        "n_pieces": n,
        "pieces_meta": [
            {
                "index": i,
                "spikes": shapes[i]["spikes"],
                "caves": shapes[i]["caves"],
                "mask_l1": float(l1s[i]),
            }
            for i in range(n)
        ],
        "cid": data.get("cid"),
    }


def _process_one_item(idx: int, item: Any, step_coarse: int, step_fine: int) -> dict:
    """Worker for a single batch item (runs in thread)."""
    if not isinstance(item, dict):
        return {"tag": f"{idx:03d}", "error": "not an object"}
    if item.get("status", "ok") != "ok":
        return {
            "tag": f"{idx:03d}",
            "cid": item.get("cid"),
            "skipped": True,
            "status": item.get("status"),
        }
    if not (item.get("bg") or item.get("background")):
        return {"tag": f"{idx:03d}", "cid": item.get("cid"), "error": "no bg"}
    try:
        r = match_puzzle(item, step_coarse=step_coarse, step_fine=step_fine)
        r["tag"] = f"{idx:03d}"
        return r
    except Exception as exc:
        return {
            "tag": f"{idx:03d}",
            "cid": item.get("cid") if isinstance(item, dict) else None,
            "error": str(exc),
        }


async def process_payload_async(
    data: Any,
    step_coarse: int = STEP_COARSE,
    step_fine: int = STEP_FINE,
) -> dict:
    """
    Process single object or batch list concurrently (up to MAX_WORKERS).
    """
    t0 = time.perf_counter()
    if isinstance(data, dict):
        items = [data]
        batch = False
    elif isinstance(data, list):
        items = data
        batch = True
    else:
        raise ValueError("Root must be object or array")

    loop = asyncio.get_running_loop()
    tasks = [
        loop.run_in_executor(
            _executor,
            _process_one_item,
            idx,
            item,
            step_coarse,
            step_fine,
        )
        for idx, item in enumerate(items)
    ]
    results = await asyncio.gather(*tasks)

    ok_n = sum(1 for r in results if "error" not in r and not r.get("skipped"))
    skipped = sum(1 for r in results if r.get("skipped"))
    total_ms = sum(r.get("timing_ms", 0) for r in results if "timing_ms" in r)
    wall_ms = (time.perf_counter() - t0) * 1000.0

    return {
        "batch": batch,
        "ok": ok_n,
        "skipped": skipped,
        "errors": len(results) - ok_n - skipped,
        "total_timing_ms": round(total_ms, 3),
        "avg_timing_ms": round(total_ms / ok_n, 3) if ok_n else None,
        "wall_ms": round(wall_ms, 3),
        "max_workers": MAX_WORKERS,
        "results": results,
    }


# ---------------------------------------------------------------------------
# FastAPI routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "max_workers": MAX_WORKERS}


@app.post("/match")
async def match_json(
    payload: Any = Body(...),
    step_coarse: int = STEP_COARSE,
    step_fine: int = STEP_FINE,
):
    """
    Accept JSON body in the same format as the original file:
    - single object: { "bg": "...", "pieces": [...], "cid": "..." }
    - or batch array: [ {...}, {...}, ... ]
    """
    if not isinstance(payload, (dict, list)):
        raise HTTPException(
            status_code=400,
            detail="Body must be a JSON object or array (same format as the input file).",
        )
    try:
        report = await process_payload_async(payload, step_coarse=step_coarse, step_fine=step_fine)
        return report
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/match/file")
async def match_file(
    file: UploadFile = File(...),
    step_coarse: int = STEP_COARSE,
    step_fine: int = STEP_FINE,
):
    """
    Upload a JSON file (same content as original script expected from disk).
    """
    try:
        raw = await file.read()
        data = load_json_bytes(raw)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON file: {e}")
    try:
        report = await process_payload_async(data, step_coarse=step_coarse, step_fine=step_fine)
        return report
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.on_event("shutdown")
def shutdown_event():
    _executor.shutdown(wait=False)


# ---------------------------------------------------------------------------
# CLI (optional: still works as script)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "puzzle_api:app",
        host="0.0.0.0",
        port=8000,
        workers=1,  # ThreadPool handles concurrency inside
        reload=False,
    )
