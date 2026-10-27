# Puzzle Match on Vercel

FastAPI + Pillow + NumPy puzzle captcha matcher for benchmarking on Vercel serverless.

## Usage

1. Open the deployed URL.
2. Upload a JSON file (same format as local CLI: `bg` + `pieces` base64, or batch array).
3. Server measures wall time from the start of processing and returns full profile per item.

## Local

```bash
pip install -r requirements.txt
uvicorn api.index:app --reload --port 8000
```

## Deploy

Linked to GitHub `nikita133724/vercel`. Push to `main` triggers deploy, or use Vercel dashboard / CLI.
