#!/usr/bin/env bash
set -euo pipefail

ensure_dir() {
  [ -d "$1" ] || mkdir -p "$1"
}

echo "[1/8] Ensure Python 3.10 virtual environment"
if [ ! -d ".venv" ]; then
  python3.10 -m venv .venv
fi

echo "[2/8] Upgrade pip tooling"
.venv/bin/python -m pip install --upgrade pip setuptools wheel

echo "[3/8] Install backend dependencies"
.venv/bin/python -m pip install -r requirements.txt

echo "[3.5/8] Install PyTorch CUDA runtime for SenseVoice"
.venv/bin/python -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124

echo "[4/8] Ensure .env exists"
if [ ! -f ".env" ]; then
  cp .env.example .env
fi

echo "[5/8] Ensure local directories"
ensure_dir models
ensure_dir models/piper
ensure_dir bin
ensure_dir bin/piper

# Fix ownership if directories were previously created by Docker (running as root)
sudo chown -R "$(id -u):$(id -g)" models bin 2>/dev/null || true

echo "[6/8] Download SenseVoiceSmall (ModelScope iic/SenseVoiceSmall)"
.venv/bin/python -c "
from modelscope import snapshot_download
import os, shutil
src = snapshot_download('iic/SenseVoiceSmall')
dst = os.path.abspath('models/SenseVoiceSmall')
shutil.rmtree(dst, ignore_errors=True)
shutil.copytree(src, dst)
print('SenseVoiceSmall ->', dst)
"

echo "[7/8] Download Piper runtime and voice"
TAR_PATH="bin/piper/piper_linux_x86_64.tar.gz"
curl -fSL "https://github.com/rhasspy/piper/releases/latest/download/piper_linux_x86_64.tar.gz" -o "$TAR_PATH"
tar -xzf "$TAR_PATH" -C "bin/piper"
rm "$TAR_PATH"

curl -fSL "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx" \
  -o "models/piper/en_US-amy-medium.onnx"
curl -fSL "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/amy/medium/en_US-amy-medium.onnx.json" \
  -o "models/piper/en_US-amy-medium.onnx.json"

curl -fSL "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/hfc_female/medium/en_US-hfc_female-medium.onnx" \
  -o "models/piper/en_US-hfc_female-medium.onnx"
curl -fSL "https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/hfc_female/medium/en_US-hfc_female-medium.onnx.json" \
  -o "models/piper/en_US-hfc_female-medium.onnx.json"

curl -fSL "https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/xiao_ya/medium/zh_CN-xiao_ya-medium.onnx" \
  -o "models/piper/zh_CN-xiao_ya-medium.onnx"
curl -fSL "https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/xiao_ya/medium/zh_CN-xiao_ya-medium.onnx.json" \
  -o "models/piper/zh_CN-xiao_ya-medium.onnx.json"

curl -fSL "https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx" \
  -o "models/piper/zh_CN-huayan-medium.onnx"
curl -fSL "https://huggingface.co/rhasspy/piper-voices/resolve/main/zh/zh_CN/huayan/medium/zh_CN-huayan-medium.onnx.json" \
  -o "models/piper/zh_CN-huayan-medium.onnx.json"

echo "[8/8] Verify Ollama and pull Gemma model"
ollama --version
ollama pull gemma4:e2b

echo "Setup complete."
echo "Start backend with: .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --ws-ping-interval 60 --ws-ping-timeout 60"
