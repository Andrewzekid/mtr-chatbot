$ErrorActionPreference = "Stop"

Write-Host "[1/5] Creating backend virtual environment with Python 3.10..."
if (Test-Path ".venv") {
  Remove-Item -Recurse -Force ".venv"
}
py -3.10 -m venv .venv

Write-Host "[2/5] Upgrading pip tooling..."
.\.venv\Scripts\python.exe -m pip install --upgrade pip setuptools wheel

Write-Host "[3/5] Installing backend requirements..."
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

$sttBackend = $env:STT_BACKEND; if (-not $sttBackend) { $sttBackend = "sensevoice" }
if ($sttBackend -eq "sensevoice") {
  Write-Host "[3.5/5] Installing PyTorch CUDA runtime for SenseVoice..."
  .\.venv\Scripts\python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124
} else {
  Write-Host "[3.5/5] STT_BACKEND=whisper - faster-whisper uses CTranslate2 (no PyTorch needed)."
  Write-Host "         Whisper large-v3 will auto-download to .\models\whisper-cache on first start."
  New-Item -ItemType Directory -Force -Path "models\whisper-cache" | Out-Null
}

Write-Host "[4/5] Creating .env from template if missing..."
if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
}

Write-Host "[5/5] Backend setup complete."
Write-Host "Next:"
Write-Host "  1) Install/start Ollama"
Write-Host "  2) Pull model: ollama pull gemma4:e2b"
Write-Host "  3) Start API: .\.venv\Scripts\python.exe -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"
