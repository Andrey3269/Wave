# Проверки установщика ВОЛНЫ на Windows (S4): WIN-2, WIN-3, WIN-4, WIN-8, WIN-9, WIN-13, WIN-15.
#
#   pwsh -File tests\installer\run_installer_checks.ps1 -Installer dist\installer\Wave-Setup-<версия>.exe `
#        -TestInstallerOk <...-webview2-ok.exe> -TestInstallerFail <...-webview2-fail.exe> -Python <python с playwright>
#
# Предназначен для одноразовой машины (CI windows-latest, виртуальная машина): ставит ВОЛНУ в профиль
# текущего пользователя (%LOCALAPPDATA%\Programs\Wave), запускает её, обновляет поверх и удаляет.
# Данные приложения во время проверок — только во временных каталогах (WAVE_DATA_DIR, WAVE_DOWNLOADS_DIR
# с меткой .wave-test-profile); исходящая сеть приложения закрыта неработающим прокси (музыкальные
# источники не нужны). Реальный каталог %LOCALAPPDATA%\Wave получает только файл-метку, по которой
# проверяется, что установщик и деинсталлятор его не трогают. На рабочем компьютере владельца не запускать:
# вместо этого — docs\engineering\checklists\S4_WINDOWS_CHECK.md.
#
# Итог: таблица статусов PASS / FAIL / BLOCKED / NOT RUN и JSON (-Results). Код возврата 1 при любом FAIL.
# Файл сохранён в UTF-8 с BOM.
param(
    [Parameter(Mandatory)] [string]$Installer,
    [Parameter(Mandatory)] [string]$TestInstallerOk,
    [Parameter(Mandatory)] [string]$TestInstallerFail,
    [Parameter(Mandatory)] [string]$Python,
    [string]$Dist = "dist\Wave",
    [string]$Work = (Join-Path ([IO.Path]::GetTempPath()) "wave-s4-checks"),
    [string]$Results = ""
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version 3
# Сбой вне шагов (параметры, подготовка) — аннотацией: журнал CI не всегда доступен
trap {
    $line = $_.InvocationInfo.ScriptLineNumber
    Write-Host "::error title=run_installer_checks fatal::line ${line}: $($_.Exception.Message -replace '[\r\n]+', ' | ')"
    exit 1
}

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
$Version = (& $Python (Join-Path "backend" "version.py")).Trim()
$AppId = "{314C4533-1390-4F2A-B0B8-7003E0F6F200}"
$UninstallKey = "Software\Microsoft\Windows\CurrentVersion\Uninstall\${AppId}_is1"
$AppDir = Join-Path $env:LOCALAPPDATA "Programs\Wave"
$StartMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "ВОЛНА (Wave).lnk"
$Desktop = Join-Path ([Environment]::GetFolderPath("Desktop")) "ВОЛНА (Wave).lnk"
$Installer = (Resolve-Path $Installer).Path
$Dist = (Resolve-Path $Dist).Path
if (-not $Results) { $Results = Join-Path $Work "s4-installer-results.json" }
if (Test-Path $Work) { Remove-Item $Work -Recurse -Force }
New-Item -ItemType Directory -Force -Path $Work | Out-Null

$Track = @{ id = "AAAAAAAAAA1"; source = "yt"; title = "S4 трек"; artist = "S4 исполнитель";
            stream_url = "/api/stream/yt/AAAAAAAAAA1" }
$Checks = [ordered]@{}

# GitHub показывает не больше 10 аннотаций каждого вида на шаг: FAIL и BLOCKED выводятся сразу,
# PASS — в конце, несколькими сводными аннотациями
function Note([string]$id, [string]$status, [string]$detail) {
    $Checks[$id] = [ordered]@{ status = $status; detail = $detail }
    Write-Host "$status  $id :: $detail"
    if ($status -eq "PASS") { return }
    $kind = if ($status -eq "FAIL") { "error" } else { "warning" }
    $one = ($detail -replace "[\r\n]+", " | ")
    $title = ("$id $status" -replace "[,:]", ";")             # запятая и двоеточие разделяют свойства аннотации
    Write-Host "::${kind} title=${title}::$one"
}

# Тело шага возвращает описание результата; строка «BLOCKED: ...» — окружение не позволяет проверить
function Step([string]$id, [scriptblock]$body) {
    Write-Host "--- $id"
    try {
        $out = (& $body) -join "; "
        if ($out -like "BLOCKED:*") { Note $id "BLOCKED" $out.Substring(8).Trim() } else { Note $id "PASS" $out }
    } catch { Note $id "FAIL" "$($_.Exception.Message)" }
}

function Assert([bool]$cond, [string]$message) { if (-not $cond) { throw $message } }

function Free-Port {
    $l = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0); $l.Start()
    $p = $l.LocalEndpoint.Port; $l.Stop(); return $p
}

function Listening([int]$port) {
    return [bool](Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue)
}

function Run-Exe([string]$exe, [string]$arguments, [int]$timeoutSec = 300) {
    $p = Start-Process -FilePath $exe -ArgumentList $arguments -PassThru
    $null = $p.Handle
    if (-not $p.WaitForExit($timeoutSec * 1000)) {
        $titles = Get-Process | Where-Object { $_.MainWindowTitle } | ForEach-Object { "$($_.ProcessName): $($_.MainWindowTitle)" }
        Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
        # процессы установщика/деинсталлятора, распакованные во временную папку (*.tmp)
        Get-Process | Where-Object { $_.Path -like "*.tmp" } | Stop-Process -Force -ErrorAction SilentlyContinue
        throw "$([IO.Path]::GetFileName($exe)) $arguments did not finish in $timeoutSec s; windows: $($titles -join '; ')"
    }
    return $p.ExitCode
}

function Setup-Args([string]$log, [string]$extra = "") {
    return "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG=`"$log`" $extra".Trim()
}

function Wait-Uninstalled([string]$dir, [int]$timeoutSec = 180) {
    # Деинсталлятор Inno Setup копирует себя во временную папку и сразу завершает исходный процесс:
    # конец удаления — это исчезновение unins000.exe и процессов-копий _iu*.tmp
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        $copies = Get-Process | Where-Object { $_.ProcessName -like "_iu*" }
        if (-not (Test-Path (Join-Path $dir "unins000.exe")) -and -not $copies) { return }
        Start-Sleep -Milliseconds 500
    }
    throw "uninstall of $dir did not finish in $timeoutSec s"
}

function Uninstall-Wave([string]$dir, [string]$log) {
    $code = Run-Exe (Join-Path $dir "unins000.exe") (Setup-Args $log) 120
    Wait-Uninstalled $dir
    return $code
}

function Tree([string]$dir) {
    # относительный путь -> SHA-256 для всех файлов
    $map = @{}
    $base = (Resolve-Path $dir).Path.TrimEnd("\") + "\"
    foreach ($f in Get-ChildItem -Path $dir -Recurse -File -Force) {
        $map[$f.FullName.Substring($base.Length)] = (Get-FileHash $f.FullName -Algorithm SHA256).Hash
    }
    return $map
}

function New-Profile([string]$name) {
    $data = Join-Path $Work "$name-data"; $dl = Join-Path $Work "$name-downloads"
    foreach ($d in $data, $dl) {
        New-Item -ItemType Directory -Force -Path $d | Out-Null
        Set-Content -Path (Join-Path $d ".wave-test-profile") -Value "s4" -Encoding utf8
    }
    return @{ data = $data; downloads = $dl }
}

function Start-Wave([string]$exe, [int]$port, $prof) {
    $psi = [Diagnostics.ProcessStartInfo]::new($exe)
    $psi.UseShellExecute = $false
    $psi.WorkingDirectory = Split-Path $exe
    $psi.Environment["WAVE_PORT"] = "$port"
    $psi.Environment["WAVE_DATA_DIR"] = $prof.data
    $psi.Environment["WAVE_DOWNLOADS_DIR"] = $prof.downloads
    # музыкальные источники не нужны: внешняя сеть приложения закрыта неработающим прокси
    foreach ($k in "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY") { $psi.Environment[$k] = "http://127.0.0.1:9" }
    $psi.Environment["NO_PROXY"] = "127.0.0.1,localhost"
    foreach ($k in "YTDLP_COOKIES", "YTDLP_PROXY", "WAVE_JS_RUNTIME") { $null = $psi.Environment.Remove($k) }
    return [Diagnostics.Process]::Start($psi)
}

function Health([int]$port, [int]$timeoutSec = 90) {
    $deadline = (Get-Date).AddSeconds($timeoutSec)
    while ((Get-Date) -lt $deadline) {
        try { return Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -NoProxy -TimeoutSec 3 } catch { Start-Sleep -Milliseconds 500 }
    }
    throw "no /api/health on port $port in $timeoutSec s"
}

function Api([int]$port, [string]$method, [string]$path, $body = $null) {
    $req = @{ Uri = "http://127.0.0.1:$port$path"; Method = $method; NoProxy = $true; TimeoutSec = 20 }
    if ($null -ne $body) {
        $req.ContentType = "application/json; charset=utf-8"
        $req.Body = [Text.Encoding]::UTF8.GetBytes(($body | ConvertTo-Json -Compress -Depth 5))
    }
    return Invoke-RestMethod @req
}

function Log-Tail($prof, [int]$n = 15) {
    $log = Join-Path $prof.data "logs\wave.log"
    if (Test-Path $log) { return (Get-Content $log -Tail $n -Encoding utf8) -join " | " } else { return "no wave.log" }
}

function Close-Wave([Diagnostics.Process]$p, $prof, [int]$timeoutSec = 40) {
    # как пользователь: закрыть окно (WM_CLOSE). Возвращает true, если процесс завершился сам.
    $deadline = (Get-Date).AddSeconds(20)
    while ($p.MainWindowHandle -eq 0 -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 300; $p.Refresh() }
    $null = $p.CloseMainWindow()
    if ($p.WaitForExit($timeoutSec * 1000)) { return $true }
    Write-Host "Wave did not close by window message; log: $(Log-Tail $prof)"
    Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    $null = $p.WaitForExit(10000)
    return $false
}

function No-Processes([string]$why) {
    Start-Sleep -Seconds 1
    $left = Get-Process -Name Wave, deno -ErrorAction SilentlyContinue
    Assert (-not $left) "$why : processes left: $(($left | ForEach-Object { "$($_.ProcessName)#$($_.Id)" }) -join ', ')"
}

$ExpectedExtras = @("THIRD_PARTY_NOTICES.txt", "unins000.exe", "unins000.dat")
$DistTree = $null
$DefaultProfileSentinel = Join-Path $env:LOCALAPPDATA "Wave\s4-sentinel-do-not-delete.txt"

# ---------------------------------------------------------------------------
Step "WIN-15 dist" {
    $roots = @($Dist, (Join-Path $Dist "_internal"))           # каталоги репозитория могли бы попасть только сюда
    $bad = Get-ChildItem -Path $Dist -Recurse -Force | Where-Object {
        $n = $_.Name
        ($_.PSIsContainer -and $n -in @("__pycache__", ".git", ".venv", ".venv-build", "node_modules", "logs")) -or
        ($_.PSIsContainer -and $n -in @("tests", "docs", "build", "dist", "installer") -and (Split-Path $_.FullName) -in $roots) -or
        (-not $_.PSIsContainer -and ($n -match '\.(db|db-wal|db-shm|db-journal|sqlite3?|log|pyc|pyo|m4a|mp3|webm|ogg|opus|wav|pem|key|pfx)$' -or
                                     $n -match '^(\.env.*|cookies.*\.txt|.*\.cookies|\.wave-test-profile|wave\.db)$') -and
         $_.FullName.Substring($Dist.Length) -ne "\_internal\certifi\cacert.pem")     # корневые сертификаты certifi — не секрет
    }
    $script:DistTree = Tree $Dist                                # нужен следующим шагам и при сбое этого
    Assert (-not $bad) "forbidden files in dist: $(($bad | Select-Object -First 15 | ForEach-Object { $_.FullName.Substring($Dist.Length) }) -join ', ')"
    $mb = ((Get-ChildItem $Dist -Recurse -File | Measure-Object Length -Sum).Sum / 1MB)
    "dist\Wave: $($DistTree.Count) files, {0:N1} MB; no databases, logs, cookies, .env, media, __pycache__ or .pyc" -f $mb
}

Step "installer file" {
    $fi = Get-Item $Installer
    Assert ($fi.Name -eq "Wave-Setup-$Version.exe") "unexpected file name $($fi.Name)"
    $vi = $fi.VersionInfo
    $pv = "$($vi.ProductVersion)".Trim([char]0, [char]32)        # Inno Setup дополняет строку пробелами
    Assert ($pv -eq $Version -and $vi.FileVersionRaw.ToString() -eq "$Version.0") "setup version resource: '$pv' / $($vi.FileVersionRaw)"
    $bytes = [IO.File]::ReadAllBytes($Installer)
    $text = [Text.Encoding]::ASCII.GetString($bytes)
    Assert ($text -match 'requestedExecutionLevel\s+level="asInvoker"') "setup manifest does not request asInvoker (would ask for admin rights)"
    Assert ($text -notmatch 'level="requireAdministrator"') "setup manifest requires administrator"
    "$($fi.Name) {0:N1} MB; version {1}; manifest asInvoker (no elevation)" -f ($fi.Length / 1MB), $pv
}

# Метка в настоящем каталоге данных: установщик и деинсталлятор не должны её трогать
New-Item -ItemType Directory -Force -Path (Split-Path $DefaultProfileSentinel) | Out-Null
Set-Content -Path $DefaultProfileSentinel -Value "S4 check $(Get-Date -Format o)" -Encoding utf8
$SentinelHash = (Get-FileHash $DefaultProfileSentinel).Hash

# ---------------------------------------------------------------------------
Step "WIN-2 install (per-user, silent)" {
    $log = Join-Path $Work "install.log"
    $code = Run-Exe $Installer (Setup-Args $log '/TASKS="desktopicon"')
    $logText = Get-Content $log -Raw -Encoding utf8
    Assert ($code -eq 0) "setup exit code $code"
    Assert (Test-Path (Join-Path $AppDir "Wave.exe")) "Wave.exe not in $AppDir"
    Assert ($logText -notmatch "TEST MODE") "release installer contains test mode"
    Assert ($logText -match "Wave: WebView2 Runtime found") "no WebView2 detection line in setup log"
    $inst = Tree $AppDir
    $extra = @($inst.Keys | Where-Object { -not $DistTree.ContainsKey($_) } | Sort-Object)
    $missing = @($DistTree.Keys | Where-Object { -not $inst.ContainsKey($_) })
    $changed = @($DistTree.Keys | Where-Object { $inst.ContainsKey($_) -and $inst[$_] -ne $DistTree[$_] })
    Assert ($missing.Count -eq 0) "not installed: $($missing | Select-Object -First 10)"
    Assert ($changed.Count -eq 0) "installed files differ from dist: $($changed | Select-Object -First 10)"
    Assert (-not (Compare-Object $extra $ExpectedExtras)) "unexpected extra files: $($extra -join ', ')"
    Assert ((Get-FileHash (Join-Path $AppDir "THIRD_PARTY_NOTICES.txt")).Hash -eq $DistTree["_internal\licenses\THIRD_PARTY_NOTICES.txt"]) "THIRD_PARTY_NOTICES.txt next to Wave.exe differs"
    # WScript.Shell не открывает ярлык с кириллицей в имени (TargetPath пустой) — читаем через Shell.Application
    $shellApp = New-Object -ComObject Shell.Application
    $exeItem = Get-Item (Join-Path $AppDir "Wave.exe")
    foreach ($lnk in $StartMenu, $Desktop) {
        Assert (Test-Path -LiteralPath $lnk) "shortcut missing: $lnk"
        $target = $shellApp.NameSpace((Split-Path $lnk)).ParseName((Split-Path $lnk -Leaf)).GetLink.Path
        # путь может прийти в коротком (8.3) виде или в другом регистре: сравнивается сам файл
        $same = $target -and (Test-Path $target) -and ((Get-Item $target).FullName -ieq $exeItem.FullName -or
                (Get-FileHash $target).Hash -eq (Get-FileHash $exeItem.FullName).Hash -and (Split-Path $target -Leaf) -ieq "Wave.exe")
        Assert $same "shortcut $lnk points to '$target', expected '$($exeItem.FullName)'"
    }
    $key = Get-ItemProperty -Path "HKCU:\$UninstallKey"
    Assert ($key.DisplayVersion -eq $Version) "HKCU uninstall DisplayVersion=$($key.DisplayVersion)"
    Assert ($key.DisplayName -eq "ВОЛНА (Wave)" -and $key.Publisher -eq "Andrey3269") "HKCU uninstall name/publisher: $($key.DisplayName) / $($key.Publisher)"
    foreach ($view in "Registry64", "Registry32") {
        $hklm = [Microsoft.Win32.RegistryKey]::OpenBaseKey("LocalMachine", $view)
        Assert ($null -eq $hklm.OpenSubKey($UninstallKey)) "per-machine uninstall entry exists ($view)"
    }
    "installed to $AppDir = dist\Wave file by file (SHA-256, $($DistTree.Count) files) + $($ExpectedExtras -join ', '); Start menu and desktop shortcuts; uninstall entry only in HKCU ($($key.DisplayName) $($key.DisplayVersion), $($key.Publisher)); WebView2 detected"
}

# ---------------------------------------------------------------------------
$P1 = Free-Port
$Prof = New-Profile "main"
$App = $null
Step "WIN-13 installed app on WAVE_PORT" {
    $script:App = Start-Wave (Join-Path $AppDir "Wave.exe") $P1 $Prof
    $h = Health $P1
    Assert ($h.status -eq "ok" -and $h.version -eq $Version) "health: $($h | ConvertTo-Json -Compress)"
    $s = Api $P1 GET "/api/settings"
    Assert ($s.paths.data -eq $Prof.data -and $s.paths.downloads -eq $Prof.downloads) "profile paths: $($s.paths | ConvertTo-Json -Compress)"
    Assert ((Log-Tail $Prof 40) -match "WAVE_PORT: $P1") "launcher did not log the fixed port"
    "installed Wave.exe answers on 127.0.0.1:$P1, version $($h.version), temporary profile"
}

Step "WIN-13 e2e against installed app" {
    $shots = Join-Path $Work "e2e-shots"
    $out = & $Python (Join-Path "tests" "e2e_smoke.py") "http://127.0.0.1:$P1" $shots 2>&1 | Out-String
    $code = $LASTEXITCODE
    Set-Content -Path (Join-Path $Work "e2e-installed.txt") -Value $out -Encoding utf8
    $m = [regex]::Match($out, "Проверок: (\d+), провалено: (\d+)")
    Assert ($code -eq 0 -and $m.Success -and $m.Groups[2].Value -eq "0") "e2e exit $code; $(($out -split "`n" | Select-Object -Last 12) -join ' | ')"
    "tests\e2e_smoke.py against installed Wave.exe: $($m.Groups[1].Value) checks, 0 failed"
}

Step "WIN-4 second launch" {
    $P2 = Free-Port
    $second = Start-Wave (Join-Path $AppDir "Wave.exe") $P2 $Prof
    Start-Sleep -Seconds 12
    $listening2 = Listening $P2
    $tail = Log-Tail $Prof 60
    $alive = -not $second.HasExited
    if ($alive) { Stop-Process -Id $second.Id -Force -ErrorAction SilentlyContinue }   # ждёт закрытия сообщения «уже запущен»
    Assert (-not $listening2) "second instance listens on $P2"
    Assert ($tail -match "второй экземпляр завершается, backend не запускается") "no second-instance line in wave.log"
    $h = Health $P1 10
    Assert ($h.status -eq "ok") "first instance not healthy after second launch"
    $ports = Get-NetTCPConnection -State Listen -OwningProcess (Get-Process -Name Wave).Id -ErrorAction SilentlyContinue | Where-Object LocalAddress -eq "127.0.0.1"
    Assert (@($ports).Count -eq 1) "Wave listens on $(@($ports).Count) ports: $(($ports | ForEach-Object LocalPort) -join ', ')"
    "second Wave.exe started no backend (port $P2 free, one listening port $P1), logged it and showed the 'already running' message (process alive: $alive, closed by the check)"
}

# Данные для проверки сохранности (через настоящий API, как это делает интерфейс)
Step "data for update/uninstall checks" {
    $null = Api $P1 PUT "/api/settings" @{ volume = 0.42; profile_name = "S4 профиль" }
    $null = Api $P1 POST "/api/favorites" $Track
    $null = Api $P1 POST "/api/history" $Track
    $pl = Api $P1 POST "/api/playlists" @{ name = "S4 плейлист" }
    $null = Api $P1 POST "/api/playlists/$($pl.id)/tracks" $Track
    [IO.File]::WriteAllBytes((Join-Path $Prof.downloads "S4 скачанный трек.m4a"), [byte[]](1..255))
    Set-Content -Path (Join-Path $Prof.data "s4-sentinel.txt") -Value "keep" -Encoding utf8
    "settings, favorite, history, playlist with a track, a file in downloads, a sentinel in the data folder"
}

# ---------------------------------------------------------------------------
Step "WIN-8 update while Wave is running" {
    $log = Join-Path $Work "update-running.log"
    $code = Run-Exe $Installer (Setup-Args $log) 180
    $logText = if (Test-Path $log) { Get-Content $log -Raw -Encoding utf8 } else { "" }
    $running = -not $App.HasExited
    $mutexLine = ($logText -split "`r?`n" | Where-Object { $_ -match "mutex|running|Restart Manager|close" } | Select-Object -First 6) -join " | "
    Set-Content -Path (Join-Path $Work "update-running-summary.txt") -Value "exit=$code running_after=$running`n$mutexLine" -Encoding utf8
    if ($running) {
        Assert ((Health $P1 10).status -eq "ok") "Wave is running but unhealthy after the update attempt"
        Assert ($code -ne 0) "setup reported success while the running Wave kept its files"
        "silent update with Wave running did not touch the running app: setup exit $code (AppMutex: interactive setup asks to close Wave; silent setup cancels); log: $mutexLine"
    } else {
        Assert ($code -eq 0) "Wave was closed but setup exit code is $code"
        "silent update closed the running Wave and installed (exit 0); log: $mutexLine"
    }
}

Step "WIN-8 graceful close before update" {
    if ($App.HasExited) { return "Wave was already closed by the update" }
    $ok = Close-Wave $App $Prof
    Assert $ok "Wave did not exit after its window was closed; log: $(Log-Tail $Prof)"
    Assert ((Log-Tail $Prof 30) -match "Wave закрыт") "no 'Wave закрыт' in wave.log"
    No-Processes "after closing the window"
    "window closed -> backend stopped -> 'Wave закрыт' in the log; no Wave.exe/deno.exe left"
}

Step "WIN-8 update over the installed version" {
    # «Старые» файлы: лишний файл в _internal и испорченный файл программы должны исчезнуть/восстановиться
    $stale = Join-Path $AppDir "_internal\s4-stale-file-from-old-version.txt"
    Set-Content -Path $stale -Value "old" -Encoding utf8
    $index = Join-Path $AppDir "_internal\frontend\index.html"
    Add-Content -Path $index -Value "<!-- modified by S4 check -->" -Encoding utf8
    $dbBefore = (Get-FileHash (Join-Path $Prof.data "wave.db")).Hash
    $log = Join-Path $Work "update.log"
    $code = Run-Exe $Installer (Setup-Args $log) 300
    Assert ($code -eq 0) "update exit code $code"
    Assert (-not (Test-Path $stale)) "stale file from the previous installation survived the update"
    Assert ((Get-FileHash $index).Hash -eq $DistTree["_internal\frontend\index.html"]) "program file was not replaced"
    $inst = Tree $AppDir
    Assert ($inst.Count -eq $DistTree.Count + $ExpectedExtras.Count) "installed file count $($inst.Count) after update"
    Assert ((Get-FileHash (Join-Path $Prof.data "wave.db")).Hash -eq $dbBefore) "the database changed during the update"
    "reinstall over the same AppId: program files replaced (stale file removed, modified file restored), data untouched"
}

Step "WIN-8 data after update and restart" {
    $script:App = Start-Wave (Join-Path $AppDir "Wave.exe") $P1 $Prof
    $h = Health $P1
    $s = Api $P1 GET "/api/settings"
    Assert ($s.values.volume -eq 0.42 -and $s.values.profile_name -eq "S4 профиль") "settings lost: $($s.values | ConvertTo-Json -Compress)"
    Assert (@((Api $P1 GET "/api/favorites").items | Where-Object id -eq $Track.id).Count -eq 1) "favorite lost"
    Assert (@((Api $P1 GET "/api/history").tracks | Where-Object id -eq $Track.id).Count -eq 1) "history lost"
    $pl = @((Api $P1 GET "/api/playlists").items | Where-Object name -eq "S4 плейлист")
    Assert ($pl.Count -eq 1 -and $pl[0].count -eq 1) "playlist lost"
    Assert (Test-Path (Join-Path $Prof.downloads "S4 скачанный трек.m4a")) "downloaded file lost"
    "after the update Wave $($h.version) starts; settings, favorites, history, playlist and downloads are intact"
}

# ---------------------------------------------------------------------------
Step "WIN-9 uninstall while Wave is running" {
    $log = Join-Path $Work "uninstall-running.log"
    $code = Run-Exe (Join-Path $AppDir "unins000.exe") (Setup-Args $log) 120
    Start-Sleep -Seconds 5
    $copies = Get-Process | Where-Object { $_.ProcessName -like "_iu*" }
    $deadline = (Get-Date).AddSeconds(60)
    while ($copies -and (Get-Date) -lt $deadline) { Start-Sleep -Seconds 1; $copies = Get-Process | Where-Object { $_.ProcessName -like "_iu*" } }
    if ($copies) {
        $copies | Stop-Process -Force -ErrorAction SilentlyContinue
        throw "silent uninstall with Wave running hung for 60 s (a message box despite /SUPPRESSMSGBOXES?)"
    }
    $stillInstalled = Test-Path (Join-Path $AppDir "Wave.exe")
    $running = -not $App.HasExited
    if ($stillInstalled) {
        Assert $running "uninstaller stopped although Wave was not running"
        Assert ((Health $P1 10).status -eq "ok") "Wave unhealthy after the uninstall attempt"
        "silent uninstall with Wave running was refused (AppMutex), app intact and running (exit $code)"
    } else {
        "silent uninstall with Wave running removed the program (Wave running after: $running)"
    }
}

Step "WIN-9 uninstall keeps user data" {
    if (-not $App.HasExited) { $null = Close-Wave $App $Prof }
    No-Processes "before uninstall"
    $before = @{ db = (Get-FileHash (Join-Path $Prof.data "wave.db")).Hash; dl = (Tree $Prof.downloads); data = (Tree $Prof.data) }
    if (Test-Path (Join-Path $AppDir "unins000.exe")) { $null = Uninstall-Wave $AppDir (Join-Path $Work "uninstall.log") }
    Assert (-not (Test-Path (Join-Path $AppDir "Wave.exe"))) "Wave.exe still installed"
    Assert (-not (Test-Path (Join-Path $AppDir "_internal"))) "_internal still installed"
    Assert (-not (Test-Path $StartMenu) -and -not (Test-Path $Desktop)) "shortcuts left"
    Assert (-not (Test-Path "HKCU:\$UninstallKey")) "uninstall entry left"
    Assert ((Get-FileHash (Join-Path $Prof.data "wave.db")).Hash -eq $before.db) "database changed by uninstall"
    $after = Tree $Prof.data
    Assert (@($before.data.Keys | Where-Object { $after[$_] -ne $before.data[$_] }).Count -eq 0) "data folder changed by uninstall"
    $afterDl = Tree $Prof.downloads
    Assert (@($before.dl.Keys | Where-Object { $afterDl[$_] -ne $before.dl[$_] }).Count -eq 0) "downloads changed by uninstall"
    Assert ((Test-Path $DefaultProfileSentinel) -and (Get-FileHash $DefaultProfileSentinel).Hash -eq $SentinelHash) "%LOCALAPPDATA%\Wave was touched"
    "program, shortcuts and HKCU entry removed; data folder ($($after.Count) files incl. wave.db), downloads and %LOCALAPPDATA%\Wave unchanged"
}

# ---------------------------------------------------------------------------
Step "WIN-3 missing WebView2, bootstrapper succeeds (test build)" {
    $dir = Join-Path $Work "wv-ok"; $log = Join-Path $Work "webview2-ok.log"
    $code = Run-Exe (Resolve-Path $TestInstallerOk).Path (Setup-Args $log "/DIR=`"$dir`"")
    $t = Get-Content $log -Raw -Encoding utf8
    Assert ($code -eq 0) "exit code $code"
    Assert ($t -match "TEST MODE TestWebView2=ok" -and $t -match "fake bootstrapper exit code 0" -and $t -match "Wave: WebView2 Runtime installed") "log lines missing"
    $null = Uninstall-Wave $dir (Join-Path $Work "webview2-ok-uninstall.log")
    "bootstrapper run and waited for (exit 0), WebView2 confirmed, setup exit 0"
}

Step "WIN-3 missing WebView2, bootstrapper fails (test build)" {
    $dir = Join-Path $Work "wv-fail"; $log = Join-Path $Work "webview2-fail.log"
    $code = Run-Exe (Resolve-Path $TestInstallerFail).Path (Setup-Args $log "/DIR=`"$dir`"")
    $t = Get-Content $log -Raw -Encoding utf8
    Assert ($code -eq 100) "expected exit code 100 (installed without WebView2), got $code"
    Assert ($t -match "fake bootstrapper exit code 1603" -and $t -match "still missing after the bootstrapper \(code 1603\)") "log lines missing"
    Assert (Test-Path (Join-Path $dir "Wave.exe")) "program not installed"
    $null = Uninstall-Wave $dir (Join-Path $Work "webview2-fail-uninstall.log")
    "bootstrapper failure (1603) is detected: message (suppressed in silent mode), launch not offered, setup exit 100"
}

# ---------------------------------------------------------------------------
Step "WIN-2 standard (non-admin) user" {
    $user = "wave-s4-" + (Get-Random -Maximum 99999)
    $pw = "S4!" + [guid]::NewGuid().ToString("N").Substring(0, 20)
    $secure = ConvertTo-SecureString $pw -AsPlainText -Force
    $shared = "C:\Users\Public\wave-s4"
    New-Item -ItemType Directory -Force -Path $shared | Out-Null
    Copy-Item $Installer $shared -Force
    icacls $shared /grant "*S-1-1-0:(OI)(CI)M" | Out-Null
    try {
        try {
            New-LocalUser -Name $user -Password $secure -AccountNeverExpires | Out-Null
            Add-LocalGroupMember -Group (Get-LocalGroup -SID "S-1-5-32-545") -Member $user  # Users, не Administrators
        } catch {
            return "BLOCKED: cannot create a local standard user here: $($_.Exception.Message)"
        }
        $cred = [pscredential]::new("$env:COMPUTERNAME\$user", $secure)
        $log = Join-Path $shared "stduser-install.log"
        # Процесс от имени другого пользователя наследует окружение этого: TEMP раннера ему недоступен,
        # и установщику негде распаковаться (код 1). Даём общую временную папку.
        # То же с путями профиля: известные папки Windows (LocalAppData и др.) раскрываются через %USERPROFILE%
        # процесса, а он унаследован от раннера — установщик получал «доступ запрещён» (0x80070005).
        $tmp = Join-Path $shared "tmp"; New-Item -ItemType Directory -Force -Path $tmp | Out-Null
        $home2 = Join-Path (Split-Path $env:USERPROFILE) $user
        $saved = @{}
        $vars = @{ TEMP = $tmp; TMP = $tmp; USERPROFILE = $home2; HOMEPATH = $home2.Substring(2); USERNAME = $user;
                   APPDATA = "$home2\AppData\Roaming"; LOCALAPPDATA = "$home2\AppData\Local" }
        foreach ($k in $vars.Keys) { $saved[$k] = [Environment]::GetEnvironmentVariable($k); [Environment]::SetEnvironmentVariable($k, $vars[$k]) }
        try {
            $p = Start-Process -FilePath (Join-Path $shared ([IO.Path]::GetFileName($Installer))) -ArgumentList (Setup-Args $log) `
                 -Credential $cred -LoadUserProfile -WorkingDirectory $shared -PassThru
        } catch {
            return "BLOCKED: cannot start a process as a local standard user here: $($_.Exception.Message)"
        } finally {
            foreach ($k in $saved.Keys) { [Environment]::SetEnvironmentVariable($k, $saved[$k]) }
        }
        $null = $p.Handle
        if (-not $p.WaitForExit(300000)) { Stop-Process -Id $p.Id -Force; throw "setup as $user did not finish" }
        $t = if (Test-Path $log) { Get-Content $log -Raw -Encoding utf8 } else { "" }
        $profileDir = (Get-CimInstance Win32_UserProfile | Where-Object { $_.LocalPath -like "*\$user*" } | Select-Object -First 1).LocalPath
        $exe = if ($profileDir) { Join-Path $profileDir "AppData\Local\Programs\Wave\Wave.exe" } else { "" }
        Assert ($p.ExitCode -eq 0) "setup exit code $($p.ExitCode) as standard user; log tail: $(($t -split "`n" | Select-Object -Last 8) -join ' | ')"
        Assert ($exe -and (Test-Path $exe)) "Wave.exe not found in the standard user's profile ($profileDir)"
        $admin = [regex]::Match($t, "Administrative install mode: (\w+)").Groups[1].Value
        Assert ($admin -ne "Yes") "setup ran in administrative mode"
        "local standard user $user (group Users): setup exit 0 without elevation, installed to $exe; log 'Administrative install mode: $admin'"
    } finally {
        Remove-LocalUser -Name $user -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------------------
Step "no Wave.exe or deno.exe left" { No-Processes "at the end"; "none" }
$sizes = [ordered]@{
    dist_mb = [math]::Round(((Get-ChildItem $Dist -Recurse -File | Measure-Object Length -Sum).Sum / 1MB), 1)
    installer_mb = [math]::Round(((Get-Item $Installer).Length / 1MB), 1)
}
$report = [ordered]@{ version = $Version; installer = [IO.Path]::GetFileName($Installer); sizes = $sizes; checks = $Checks }
$report | ConvertTo-Json -Depth 5 | Set-Content -Path $Results -Encoding utf8
Write-Host ""
Write-Host ("{0,-60} {1}" -f "check", "status")
foreach ($k in $Checks.Keys) { Write-Host ("{0,-60} {1}" -f $k, $Checks[$k].status) }
Write-Host ("sizes: dist\Wave {0} MB, installer {1} MB" -f $sizes.dist_mb, $sizes.installer_mb)
$passed = @($Checks.Keys | Where-Object { $Checks[$_].status -eq "PASS" } | ForEach-Object { "[$_] $($Checks[$_].detail)" })
for ($i = 0; $i -lt $passed.Count; $i += 3) {
    $chunk = ($passed[$i..([math]::Min($i + 2, $passed.Count - 1))] -join " || ") -replace "[\r\n]+", " | "
    Write-Host "::notice title=S4 PASS ($([int]($i / 3) + 1))::$chunk"
}
$line = ($Checks.Keys | ForEach-Object { "$_=$($Checks[$_].status)" }) -join "; "
Write-Host "::notice title=S4 installer checks::$($line -replace ',', ' ')"
Write-Host "::notice title=S4 sizes::dist\Wave $($sizes.dist_mb) MB; $($report.installer) $($sizes.installer_mb) MB"
$failed = @($Checks.Keys | Where-Object { $Checks[$_].status -eq "FAIL" })
if ($failed) { Write-Host "FAILED: $($failed -join ', ')"; exit 1 }
exit 0
