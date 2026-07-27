#!/usr/bin/env bash
set -euo pipefail

echo "[1/5] Creating backend virtual environment with Python 3.10..."
if [ -d ".venv" ]; then
  rm -rf .venv
fi
python3.10 -m venv .venv

echo "[2/5] Upgrading pip tooling..."
.venv/bin/python -m pip install --upgrade pip setuptools wheel

echo "[3/5] Installing backend requirements..."
.venv/bin/python -m pip install -r requirements.txt

echo "[3.5/5] Installing PyTorch CUDA runtime for SenseVoice..."
.venv/bin/python -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124

echo "[4/5] Creating .env from template if missing..."
if [ ! -f ".env" ]; then
  cp .env.example .env
fi

echo "[5/5] Backend setup complete."
echo "Next:"
echo "  1) Install/start Ollama"
echo "  2) Pull model: ollama pull gemma4:e2b"
echo "  3) Start API: .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"
