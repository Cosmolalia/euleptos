# Euleptos one-shot installer (Windows / PowerShell)
# Usage:
#   irm https://euleptos.com/install.ps1 | iex
#   .\install.ps1
#
# Environment overrides:
#   $env:EULEPTOS_DIR       install location           (default: $HOME\euleptos)
#   $env:EULEPTOS_MODEL     Ollama model to pull       (default: llama3.2:3b)
#   $env:EULEPTOS_NO_OLLAMA 1=skip Ollama install
#   $env:EULEPTOS_NO_PULL   1=skip pulling a model
#   $env:EULEPTOS_YES       1=auto-yes to all prompts (also implied if non-interactive)

$ErrorActionPreference = 'Stop'

$InstallDir   = if ($env:EULEPTOS_DIR)   { $env:EULEPTOS_DIR }   else { Join-Path $HOME 'euleptos' }
$ZipUrl       = if ($env:EULEPTOS_ZIP_URL) { $env:EULEPTOS_ZIP_URL } else { 'https://euleptos.com/dist/euleptos-latest.zip' }
$DefaultModel = if ($env:EULEPTOS_MODEL) { $env:EULEPTOS_MODEL } else { 'llama3.2:3b' }

# Auto-yes when non-interactive (irm | iex) or when EULEPTOS_YES=1
$Interactive = $true
if ($env:EULEPTOS_YES -eq '1') { $Interactive = $false }
if (-not [Environment]::UserInteractive) { $Interactive = $false }

function Say  ($m) { Write-Host $m -ForegroundColor Cyan }
function Ok   ($m) { Write-Host "  [ok] $m" -ForegroundColor Green }
function Warn ($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Dim  ($m) { Write-Host "  $m" -ForegroundColor DarkGray }
function Die  ($m) { Write-Host "  [x]  $m" -ForegroundColor Red; exit 1 }

function AskYes ($prompt) {
    if (-not $Interactive) { return $true }
    $reply = Read-Host "    $prompt [Y/n]"
    if ($reply -match '^(n|no)$') { return $false }
    return $true
}

Write-Host ''
Write-Host '+----------------------------------------+' -ForegroundColor Cyan
Write-Host '|     EULEPTOS  one-shot installer       |' -ForegroundColor Cyan
Write-Host '|     local-first AI harness             |' -ForegroundColor Cyan
Write-Host '+----------------------------------------+' -ForegroundColor Cyan

# --------------------------------------------------------- 1. Python
Say "`n-> Checking Python"
$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python3 -ErrorAction SilentlyContinue }
if (-not $python) {
    Write-Host "  Python not found. Install Python 3.10+ first:" -ForegroundColor Red
    Write-Host "    https://www.python.org/downloads/  (be sure to check 'Add Python to PATH')"
    Die "Aborted."
}
$pyExe = $python.Source
$pyVersion = & $pyExe -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
$pyOk = & $pyExe -c 'import sys; print(1 if sys.version_info>=(3,10) else 0)'
if ($pyOk -ne '1') { Die "Python $pyVersion found, but 3.10+ required." }
Ok "Python $pyVersion"

& $pyExe -m pip --version > $null 2>&1
if ($LASTEXITCODE -ne 0) { Die "pip not available. Reinstall Python with pip enabled." }

# --------------------------------------------------------- 2. Download
Say "`n-> Downloading Euleptos"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$tmpZip = Join-Path $env:TEMP "euleptos-$(Get-Random).zip"
try {
    Invoke-WebRequest -Uri $ZipUrl -OutFile $tmpZip -UseBasicParsing
} catch {
    Die "Download failed: $_"
}
$zipSize = "{0:N0} KB" -f ((Get-Item $tmpZip).Length / 1KB)
Ok "Downloaded $zipSize"

try {
    Expand-Archive -Path $tmpZip -DestinationPath $InstallDir -Force
} catch {
    Die "Unzip failed: $_"
}
Remove-Item $tmpZip -Force -ErrorAction SilentlyContinue
Ok "Extracted to $InstallDir"

# --------------------------------------------------------- 3. Python deps
Say "`n-> Installing Python dependencies"
$reqFile = Join-Path $InstallDir 'requirements.txt'
if (Test-Path $reqFile) {
    & $pyExe -m pip install --quiet --user -r $reqFile
} else {
    & $pyExe -m pip install --quiet --user fastapi uvicorn python-multipart websockets
}
if ($LASTEXITCODE -ne 0) { Die "pip install failed." }
Ok "Deps installed"

# --------------------------------------------------------- 4. Claude Code detection
$claudeAvailable = $false
Say "`n-> Checking Claude Code"
$claude = Get-Command claude -ErrorAction SilentlyContinue
if ($claude) {
    $ccVersion = try { (& claude --version 2>$null | Select-Object -First 1) } catch { 'installed' }
    Ok "Claude Code detected ($ccVersion)"
    Dim "Euleptos will drive it directly — no API key needed."
    $claudeAvailable = $true
} else {
    Warn "Claude Code not found on PATH"
    Dim "Euleptos primarily drives your existing 'claude' CLI — no API key required."
    Dim "Install it from: https://docs.anthropic.com/en/docs/claude-code"
    Dim "(You can still run Euleptos with Ollama-only — skip this and continue.)"
}

# --------------------------------------------------------- 5. Ollama
$ollamaAvailable = $false
if ($env:EULEPTOS_NO_OLLAMA -ne '1') {
    Say "`n-> Checking Ollama (local model runner)"
    $ollama = Get-Command ollama -ErrorAction SilentlyContinue
    if ($ollama) {
        Ok "Ollama already installed"
        $ollamaAvailable = $true
    } else {
        Warn "Ollama not found"
        if (AskYes "Install Ollama? (lets you run local models offline)") {
            Dim "Downloading official Ollama installer..."
            $ollamaInstaller = Join-Path $env:TEMP "OllamaSetup.exe"
            try {
                Invoke-WebRequest -Uri 'https://ollama.com/download/OllamaSetup.exe' -OutFile $ollamaInstaller -UseBasicParsing
                Dim "Running installer (it may open a window — click through)..."
                Start-Process -FilePath $ollamaInstaller -ArgumentList '/SILENT' -Wait
                Remove-Item $ollamaInstaller -Force -ErrorAction SilentlyContinue
                # Refresh PATH
                $env:Path = [System.Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [System.Environment]::GetEnvironmentVariable('Path','User')
                if (Get-Command ollama -ErrorAction SilentlyContinue) {
                    Ok "Ollama installed"
                    $ollamaAvailable = $true
                } else {
                    Warn "Ollama installer ran but command not found in PATH. Open a new terminal after this and Ollama should appear."
                }
            } catch {
                Warn "Ollama install failed: $_"
                Dim "Install manually: https://ollama.com/download/OllamaSetup.exe"
            }
        } else {
            Dim "Skipped. Install later: https://ollama.com/download/OllamaSetup.exe"
        }
    }
} else {
    Dim "Skipping Ollama (EULEPTOS_NO_OLLAMA=1)"
}

# --------------------------------------------------------- 5. Verify Ollama serving
if ($ollamaAvailable) {
    try {
        Invoke-WebRequest -Uri 'http://localhost:11434/api/tags' -UseBasicParsing -TimeoutSec 3 | Out-Null
        Ok "Ollama serving on http://localhost:11434"
    } catch {
        Dim "Ollama installed but service not detected. On Windows, Ollama usually runs as a tray service after install."
        Dim "If models don't appear in Euleptos, open the Ollama tray app or run: ollama serve"
    }
}

# --------------------------------------------------------- 6. Pull a model
if ($ollamaAvailable -and ($env:EULEPTOS_NO_PULL -ne '1')) {
    $existing = 0
    try {
        $tags = Invoke-RestMethod -Uri 'http://localhost:11434/api/tags' -TimeoutSec 5
        $existing = ($tags.models | Measure-Object).Count
    } catch {}
    if ($existing -eq 0) {
        Say "`n-> No local models yet"
        if (AskYes "Pull $DefaultModel (~2 GB, fast baseline)?") {
            & ollama pull $DefaultModel
            if ($LASTEXITCODE -eq 0) {
                Ok "$DefaultModel ready"
            } else {
                Warn "Model pull failed (try manually: ollama pull $DefaultModel)"
            }
        } else {
            Dim "Skipped. Pull any model with: ollama pull <model>"
            Dim "Model catalog: https://ollama.com/library"
        }
    } else {
        Ok "$existing Ollama model(s) already installed"
    }
}

# --------------------------------------------------------- 7. .env
Say "`n-> Configuring optional API key"
$envFile = Join-Path $InstallDir '.env'
if (Test-Path $envFile) {
    Ok ".env already exists, leaving alone"
} else {
    @"
# Anthropic API key — OPTIONAL.
#
# Euleptos uses your existing Claude Code install by default (no key needed).
# Leave this blank unless you specifically want "Pure Mode" — a bypass that
# hits the raw Anthropic API directly instead of going through claude -p.
#
#   Get one (only if you want Pure Mode): https://console.anthropic.com/
ANTHROPIC_API_KEY=
"@ | Out-File -FilePath $envFile -Encoding utf8 -NoNewline
    Ok "Created .env stub (API key optional — leave blank for default Claude Code flow)"
}

# --------------------------------------------------------- 8. Done
$port = 8080
Write-Host ''
Write-Host '+----------------------------------------+' -ForegroundColor Green
Write-Host '|        [ok] INSTALL COMPLETE           |' -ForegroundColor Green
Write-Host '+----------------------------------------+' -ForegroundColor Green
Write-Host ''
Write-Host '  Start the harness:'
Write-Host "    cd $InstallDir" -ForegroundColor Cyan
Write-Host "    .\start_dist.bat   (or: $pyExe server.py)" -ForegroundColor Cyan
Write-Host ''
Write-Host "  Then open: http://localhost:$port" -ForegroundColor Cyan
Write-Host ''
if ($claudeAvailable) {
    Dim "Claude Code is wired in — no API key needed. Just go."
} else {
    Dim "Install Claude Code (no API key needed) to use Claude: https://docs.anthropic.com/en/docs/claude-code"
}
if ($ollamaAvailable) {
    Dim "Ollama models appear in the picker as ollama:<name>."
}
Write-Host ''
