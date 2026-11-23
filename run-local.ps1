# Run BotoKo on this laptop with one command:
#     powershell -ExecutionPolicy Bypass -File .\run-local.ps1
#
# 1. Starts the private BotoKo database (folder C:\Users\<you>\botoko-db, port 5433, no password,
#    only reachable from this laptop) if it isn't running yet, e.g. after a restart.
# 2. Sets the settings BotoKo needs (only for this window).
# 3. Starts BotoKo at http://localhost:5000 with 1 worker PROCESS per CPU core (serve.py + Waitress),
#    e.g. 4 cores -> 4 worker processes x 8 threads. Stop it with Ctrl+C.
#
# Options:
#   -Simple              old way: Flask development server, 1 process (same as "py app.py")
#   -Port 5001           use another port
#   -Database botoko     use another database on the private server. The DEFAULT is botoko_live:
#                        the clean database for the real election (no sample data). The old databases
#                        are kept as backups: "botoko" (the earlier one with the sample/test students)
#                        and "botoko_demo" (the demo events).
#                        Example: .\run-local.ps1 -Database botoko     (back to the old database)
#   -DatabaseUrl "..."   use a database somewhere else (e.g. a test database)
param(
    [switch]$Simple,
    [int]$Port = 5000,
    [string]$Database = "botoko_live",
    [string]$DatabaseUrl = ""
)

$pgBin  = "C:\Program Files\PostgreSQL\18\bin"
$dbDir  = Join-Path $HOME "botoko-db"
$dbPort = 5433

function Test-DbAnswers {
    # Not just "is it running" but "does it actually answer a connection".
    & "$pgBin\pg_isready.exe" -h localhost -p $dbPort -U botoko -d botoko -t 5 *> $null
    return ($LASTEXITCODE -eq 0)
}

function Start-Db {
    Write-Host "Starting the BotoKo database on port $dbPort..."
    # Started in its own hidden window, so closing this terminal doesn't break the database.
    # (No -Wait: that would wait for the database itself to exit, i.e. forever.)
    $log = Join-Path $dbDir "log.txt"
    Start-Process -FilePath "$pgBin\pg_ctl.exe" -WindowStyle Hidden `
        -ArgumentList @("-D", "`"$dbDir`"", "-o", "`"-p $dbPort`"", "-l", "`"$log`"", "start")
    for ($i = 0; $i -lt 30; $i++) {                     # wait up to ~30 s until it answers
        Start-Sleep -Seconds 1
        if (Test-DbAnswers) { return }
    }
}

if ($DatabaseUrl) {
    Write-Host "Using the database given with -DatabaseUrl."
    $env:DATABASE_URL = $DatabaseUrl
} else {
    if (-not (Test-Path $dbDir)) {
        Write-Host "The private database folder $dbDir doesn't exist yet." -ForegroundColor Red
        Write-Host "Create it once (see README, 'Patakbuhin sa sariling laptop'), then run this again."
        exit 1
    }
    & "$pgBin\pg_ctl.exe" -D $dbDir status *> $null
    $running = ($LASTEXITCODE -eq 0)
    if ($running -and (Test-DbAnswers)) {
        Write-Host "BotoKo database is already running."
    } else {
        if ($running) {
            Write-Host "The database is running but not answering. Restarting it..." -ForegroundColor Yellow
            & "$pgBin\pg_ctl.exe" -D $dbDir -m immediate stop *> $null
            Start-Sleep -Seconds 2
        }
        Start-Db
        if (-not (Test-DbAnswers)) {
            Write-Host "The database didn't start. See $dbDir\log.txt" -ForegroundColor Red
            exit 1
        }
    }
    if ($Database -notmatch '^[A-Za-z0-9_]+$') {
        Write-Host "Database names may only use letters, numbers and _." -ForegroundColor Red
        exit 1
    }
    # Create the database the first time (empty: BotoKo makes its tables on start). Never deletes anything.
    $exists = & "$pgBin\psql.exe" -h localhost -p $dbPort -U botoko -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = '$Database'"
    if ("$exists".Trim() -ne "1") {
        Write-Host "Creating the new, empty database '$Database'..." -ForegroundColor Yellow
        & "$pgBin\createdb.exe" -h localhost -p $dbPort -U botoko $Database
        if ($LASTEXITCODE -ne 0) { Write-Host "Couldn't create database $Database." -ForegroundColor Red; exit 1 }
    }
    Write-Host "Database: $Database"
    $env:DATABASE_URL = "postgresql://botoko@localhost:$dbPort/$Database"
}

# Admin password (Voter ID "admin" on the login page). CHANGE "admin123" to a long password before the real
# election: BotoKo shows a warning while it is weak (common, or shorter than 10 characters).
if (-not $env:ADMIN_PASSWORD) { $env:ADMIN_PASSWORD = "admin123" }
$env:COOKIE_SECURE  = "0"
# Fixed SECRET_KEY (signs logins + CSRF tokens): restarting BotoKo doesn't log anyone out or expire open pages.
# Made once and kept in .secret_key next to this script (in .gitignore: never commit or share it).
if (-not $env:SECRET_KEY) {
    $keyFile = Join-Path $PSScriptRoot ".secret_key"
    if (-not (Test-Path $keyFile)) {
        $bytes = New-Object byte[] 32
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $newKey = ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
        [System.IO.File]::WriteAllText($keyFile, $newKey, [System.Text.Encoding]::ASCII)
        Write-Host "Made a new secret key in .secret_key (kept for every restart)."
    }
    $env:SECRET_KEY = ([System.IO.File]::ReadAllText($keyFile)).Trim()
}
$env:PORT           = "$Port"

Write-Host ""
Write-Host "BotoKo: http://localhost:$Port   Admin: log in on the normal login page with Voter ID 'admin' and the admin password" -ForegroundColor Green
Write-Host "Students: open the site and tap Create account. Verify them in Admin > Accounts.   Stop with Ctrl+C" -ForegroundColor Green
Write-Host ""
Set-Location $PSScriptRoot

if ($Simple) {
    py app.py                       # old way: 1 process (Flask development server)
    exit $LASTEXITCODE
}

# Multi-process server: Waitress in 1 worker process per CPU core (see serve.py).
py -c "import waitress" *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing Waitress (Windows web server, one time only)..."
    py -m pip install --quiet "waitress>=3.0"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Couldn't install Waitress. Starting the simple 1-process server instead." -ForegroundColor Yellow
        py app.py
        exit $LASTEXITCODE
    }
}
py serve.py
