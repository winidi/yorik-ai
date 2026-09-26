<#
  Yorik for Windows 10/11: sets up Docker Desktop if needed, starts the
  all-in-one Yorik stack (deploy/compose.yaml), and opens Yorik in the
  browser. Double-click Yorik-Setup.cmd, or in PowerShell:

    powershell -ExecutionPolicy Bypass -File install-windows.ps1

  What it does, in order:
    1. Admin rights (asks once), 64-bit Windows 10 22H2 or 11, 16 GB RAM advised
    2. WSL2 and Docker Desktop, via winget, if missing (may need one restart;
       it continues by itself after you sign in again)
    3. Yorik's files to %LOCALAPPDATA%\Yorik, settings with fresh passwords
    4. docker compose up, wait until Yorik answers
    5. A "Yorik" shortcut on the desktop, Yorik opens in the browser

  -Source <folder>  use a local copy of deploy/ (or a whole checkout) instead
                    of downloading the latest release
  -Version <tag>    run this release instead of "stable"
#>
param(
  [string]$Source = "",
  [string]$Version = "stable",
  [switch]$Resumed
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$Repo = "winidi/yorik-ai"
$Home_ = Join-Path $env:LOCALAPPDATA "Yorik"
$Backups = Join-Path ([Environment]::GetFolderPath("MyDocuments")) "Yorik Backups"

function Say($t)  { Write-Host "  > $t" -ForegroundColor Cyan }
function Ok($t)   { Write-Host "  v $t" -ForegroundColor Green }
function Warn($t) { Write-Host "  ! $t" -ForegroundColor Yellow }
function Fail($t, $fix) {
  Write-Host "  x $t" -ForegroundColor Red
  if ($fix) { Write-Host "    $fix" }
  Read-Host "Press Enter to close"
  exit 1
}
function Hex($bytes) {
  $b = New-Object byte[] $bytes
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($b)
  ($b | ForEach-Object { $_.ToString("x2") }) -join ""
}

Write-Host "`n  Yorik for Windows`n" -ForegroundColor White

# -- 1. admin, Windows version, memory --------------------------------
$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Say "asking for administrator rights (needed once for Docker)"
  $args_ = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Version $Version"
  if ($Source) { $args_ += " -Source `"$Source`"" }
  Start-Process powershell -Verb RunAs -ArgumentList $args_
  exit 0
}
$build = [Environment]::OSVersion.Version.Build
if (-not [Environment]::Is64BitOperatingSystem -or $build -lt 19045) {
  Fail "Yorik needs 64-bit Windows 10 22H2 (build 19045) or Windows 11." "Run Windows Update, then start this again."
}
$ramGB = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
if ($ramGB -lt 12) { Warn "this PC has $ramGB GB of memory; Yorik runs best with 16 GB or more" }
Ok "Windows build $build, $ramGB GB memory"

# -- 2. WSL2 + Docker Desktop -----------------------------------------
$dockerExe = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
if (-not (Test-Path $dockerExe)) {
  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Fail "winget (App Installer) is missing." "Install 'App Installer' from the Microsoft Store, then start this again."
  }
  wsl.exe --status *> $null
  if ($LASTEXITCODE -ne 0) {
    Say "turning on WSL2 (Windows' Linux layer, which Docker uses)"
    wsl.exe --install --no-distribution
  }
  Say "installing Docker Desktop (free for personal use; its license applies)"
  winget install -e --id Docker.DockerDesktop --accept-package-agreements --accept-source-agreements --silent
  if (-not (Test-Path $dockerExe)) { Fail "Docker Desktop didn't install." "Install it from https://www.docker.com/products/docker-desktop and start this again." }
  Ok "Docker Desktop installed"
  # A restart is needed after turning on WSL/virtualisation. Continue
  # automatically after the next sign-in.
  $runOnce = "HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce"
  $cmd = "powershell -NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Resumed -Version $Version"
  if ($Source) { $cmd += " -Source `"$Source`"" }
  New-ItemProperty -Path $runOnce -Name "YorikSetup" -Value $cmd -PropertyType String -Force | Out-Null
  Write-Host ""
  Write-Host "  Windows needs one restart to finish setting up Docker." -ForegroundColor White
  Write-Host "  After you sign in again, Yorik's setup continues by itself." -ForegroundColor White
  if ((Read-Host "  Restart now? [Y/n]") -notmatch '^[nN]') { Restart-Computer -Force }
  exit 0
}
# Docker Desktop starts with Windows from now on (Yorik lives in it).
$dsettings = Join-Path $env:APPDATA "Docker\settings-store.json"
try {
  $j = if (Test-Path $dsettings) { Get-Content $dsettings -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
  $j | Add-Member -NotePropertyName AutoStart -NotePropertyValue $true -Force
  $j | ConvertTo-Json -Depth 10 | Set-Content $dsettings -Encoding UTF8
} catch { Warn "couldn't set Docker Desktop to start with Windows; turn it on in Docker Desktop -> Settings" }

Say "starting Docker Desktop"
docker info *> $null
if ($LASTEXITCODE -ne 0) { Start-Process $dockerExe }
$deadline = (Get-Date).AddMinutes(10)
do {
  Start-Sleep 5
  docker info *> $null
} until ($LASTEXITCODE -eq 0 -or (Get-Date) -gt $deadline)
if ($LASTEXITCODE -ne 0) { Fail "Docker Desktop didn't start within 10 minutes." "Open Docker Desktop once (accept its terms if it asks), then start this again." }
Ok "Docker is running"

# -- 3. Yorik's files and settings ------------------------------------
New-Item -ItemType Directory -Force -Path $Backups | Out-Null
# A whole checkout without a published image for this version: build the
# images from it and run in place (compose.build.yaml needs the source).
$needBuild = $false
if ($Source -and (Test-Path (Join-Path $Source "Dockerfile"))) {
  docker manifest inspect "ghcr.io/winidi/yorik-ai:$Version" *> $null
  $needBuild = ($LASTEXITCODE -ne 0)
}
if ($needBuild) {
  $Home_ = Join-Path $Source "deploy"
  Warn "no published image for '$Version' - building from the checkout (about 10 minutes)"
} elseif ($Source) {
  $deploy = if (Test-Path (Join-Path $Source "deploy\compose.yaml")) { Join-Path $Source "deploy" } else { $Source }
  New-Item -ItemType Directory -Force -Path $Home_ | Out-Null
  Say "using Yorik's files from $deploy"
  Copy-Item -Recurse -Force (Join-Path $deploy "*") $Home_
} else {
  New-Item -ItemType Directory -Force -Path $Home_ | Out-Null
  Say "downloading Yorik ($Version)"
  $zip = Join-Path $env:TEMP "yorik-deploy.zip"
  $url = if ($Version -eq "stable") { "https://github.com/$Repo/releases/latest/download/yorik-deploy.zip" }
         else { "https://github.com/$Repo/releases/download/$Version/yorik-deploy.zip" }
  try { Invoke-WebRequest $url -OutFile $zip -UseBasicParsing }
  catch { Fail "couldn't download Yorik ($url)." "Check the internet connection, then start this again." }
  Expand-Archive -Force $zip $Home_
  Remove-Item $zip
}
$envFile = Join-Path $Home_ ".env"
$files = "compose.yaml"
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) { $files += ";compose.gpu.yaml"; Ok "the AI model will use the NVIDIA GPU" }
if ($needBuild) { $files += ";compose.build.yaml" }
if (-not (Test-Path $envFile)) {
  $tz = try { (Get-TimeZone).Id } catch { "W. Europe Standard Time" }
  $iana = @{ "W. Europe Standard Time" = "Europe/Berlin"; "Central Europe Standard Time" = "Europe/Budapest"; "GMT Standard Time" = "Europe/London"; "Romance Standard Time" = "Europe/Paris" }[$tz]
  $t = Get-Content (Join-Path $Home_ "env.template") -Raw
  $vals = @{
    "YORIK_VERSION" = $Version; "TZ" = $(if ($iana) { $iana } else { "Europe/Berlin" })
    "YORIK_DB_PASSWORD" = (Hex 24); "YORIK_JWT_SECRET" = (Hex 32); "IMMICH_DB_PASSWORD" = (Hex 24)
    "PAPERLESS_DB_PASSWORD" = (Hex 24); "PAPERLESS_SECRET_KEY" = (Hex 32); "PAPERLESS_ADMIN_PASSWORD" = (Hex 12)
    "PAPERLESS_YORIK_TOKEN" = (Hex 24); "YORIK_WA_BRIDGE_TOKEN" = (Hex 24)
    "YORIK_BACKUP_DIR" = ($Backups -replace '\\', '/')
  }
  foreach ($k in $vals.Keys) { $t = $t -replace "(?m)^$k=.*$", "$k=$($vals[$k])" }
  $t += "`r`nCOMPOSE_FILE=$files`r`nCOMPOSE_PATH_SEPARATOR=;`r`n"
  Set-Content -Path $envFile -Value $t -Encoding ASCII
  Ok "settings created with fresh passwords ($envFile)"
} else {
  Ok "keeping the existing settings and passwords"
}

# -- 4. start ---------------------------------------------------------
Push-Location $Home_
Say "starting Yorik (the first start downloads several GB; later starts take seconds)"
if ($needBuild) { docker compose build yorik whatsapp-bridge }
docker compose up -d --quiet-pull
if ($LASTEXITCODE -ne 0) { Pop-Location; Fail "starting the containers failed." "In a terminal: cd `"$Home_`"; docker compose logs" }
Pop-Location
Say "waiting for Yorik (first start: up to 20 minutes)"
$deadline = (Get-Date).AddMinutes(25)
$up = $false
do {
  Start-Sleep 10
  try { $up = (Invoke-WebRequest "http://localhost:8000/api/health" -UseBasicParsing -TimeoutSec 3).StatusCode -eq 200 } catch { $up = $false }
} until ($up -or (Get-Date) -gt $deadline)
if (-not $up) { Warn "Yorik isn't answering yet; it may still be setting up. It opens in the browser anyway." } else { Ok "Yorik is answering" }

# -- 5. shortcut, browser ---------------------------------------------
$lnk = Join-Path ([Environment]::GetFolderPath("Desktop")) "Yorik.url"
"[InternetShortcut]`r`nURL=http://localhost:8000/r/`r`n" | Set-Content $lnk -Encoding ASCII
Ok "shortcut 'Yorik' on the desktop"
Start-Process "http://localhost:8000/r/"
Write-Host ""
Write-Host "  Yorik is ready. Create your account in the browser window." -ForegroundColor Green
Write-Host "  Phones at home: open http://$((Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.PrefixOrigin -in 'Dhcp','Manual' -and $_.IPAddress -notlike '169.*' } | Select-Object -First 1).IPAddress):8000"
Write-Host "  On the go: in Yorik, Settings -> System -> Phones -> sign in to Tailscale."
Write-Host "  Backups go to: $Backups"
Write-Host "  Note: while this PC sleeps, Yorik sleeps too. For a family box that's always on, see docs/INSTALL.md."
Write-Host ""
if (-not $Resumed) { Read-Host "Press Enter to close" }
