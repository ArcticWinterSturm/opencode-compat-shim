# Launch the OpenCode compat shim, detached, idempotent.
# Picks a python that actually has aiohttp (bare `python` on this box can be the
# Hermes tools build, which lacks it -> ModuleNotFoundError at import).
param([int]$Port = 18788)

$src    = 'C:\Users\User\Documents\Shim_v2\opencode_proxy.py'
$log    = 'C:\Users\User\AppData\Local\Temp\oc_proxy.log'
$err    = 'C:\Users\User\AppData\Local\Temp\oc_proxy.err'
$slog   = 'C:\Users\User\AppData\Local\Temp\oc_starter.log'
$cands  = @(
    'C:\Users\User\AppData\Local\Programs\Python\Python314\python.exe',
    'C:\Users\User\AppData\Local\Programs\Python\Python310\python.exe',
    'C:\Users\User\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe'
)

# Add-Content opens/writes/closes, so two starters racing at logon cannot
# collide the way a cmd.exe ">>" redirect does (it opens exclusively).
function Log($m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $m
    try { Add-Content -Path $slog -Value $line } catch { }
    Write-Host $line
}

# Already healthy? Then there is nothing to do.
try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
    Log "already listening on $Port (default=$($h.default_model)) - no-op"
    exit 0
} catch { }

$py = $null
foreach ($c in $cands) {
    if (Test-Path $c) {
        & $c -c "import aiohttp" 2>$null
        if ($LASTEXITCODE -eq 0) { $py = $c; break }
    }
}
if (-not $py) { Log 'FATAL: no python with aiohttp found'; exit 1 }

Log "interpreter: $py"
Start-Process -WindowStyle Hidden $py -ArgumentList $src, '--port', $Port `
    -RedirectStandardOutput $log -RedirectStandardError $err

for ($i = 1; $i -le 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 3
        Log "up after ${i}s (default=$($h.default_model), $($h.free_models.Count) free models)"
        exit 0
    } catch { }
}

Log 'FAILED to start'
if (Test-Path $err) { Get-Content $err -Tail 20 | ForEach-Object { Log "  $_" } }
exit 1
