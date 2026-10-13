# Загрузка официального bootstrapper Microsoft Edge WebView2 Runtime для установщика ВОЛНЫ (S4).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File packaging\fetch_webview2.ps1 [-Dest <файл>]
#
# Bootstrapper (~2 МБ) Microsoft регулярно обновляет по одной и той же ссылке, поэтому закреплённого
# SHA-256 у него нет и выдумывать его нельзя. Подлинность проверяется подписью Authenticode:
# подпись действительна (Status = Valid) и выдана Microsoft Corporation. Проверка выполняется и для
# ранее скачанного файла. Любая ошибка — код возврата 1 и понятное сообщение; файл не остаётся
# на месте назначения, если проверка не пройдена.
# Совместим с Windows PowerShell 5.1 и PowerShell 7. Файл сохранён в UTF-8 с BOM (для 5.1).
param(
    [string]$Dest = "build\webview2\MicrosoftEdgeWebview2Setup.exe",
    # Ссылка Microsoft на Evergreen Bootstrapper (та же, что WEBVIEW2_URL в backend\launcher.py)
    [string]$Url = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
)
$ErrorActionPreference = "Stop"

function Fail([string]$message) {
    Write-Host "ОШИБКА: $message"
    exit 1
}

function Test-MicrosoftSignature([string]$path) {
    $sig = Get-AuthenticodeSignature -FilePath $path
    $subject = if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { "" }
    $ok = ($sig.Status -eq "Valid") -and ($subject -match "(^|,\s*)O=Microsoft Corporation(,|$)")
    return @{ ok = $ok; status = "$($sig.Status)"; subject = $subject; message = "$($sig.StatusMessage)" }
}

$Dest = [IO.Path]::GetFullPath((Join-Path (Get-Location) $Dest))
$dir = Split-Path $Dest
New-Item -ItemType Directory -Force -Path $dir | Out-Null      # каталог создаётся до загрузки

if (Test-Path $Dest) {
    $check = Test-MicrosoftSignature $Dest
    if ($check.ok) {
        Write-Host "WebView2 bootstrapper уже скачан и подписан Microsoft: $Dest"
        exit 0
    }
    Write-Host "Ранее скачанный bootstrapper не прошёл проверку подписи ($($check.status)), скачиваю заново"
    Remove-Item $Dest -Force
}

$tmp = "$Dest.download"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    Write-Host "Загрузка WebView2 bootstrapper: $Url"
    $ProgressPreference = "SilentlyContinue"                     # иначе Invoke-WebRequest в 5.1 очень медленный
    Invoke-WebRequest -Uri $Url -OutFile $tmp -UseBasicParsing
} catch {
    if (Test-Path $tmp) { Remove-Item $tmp -Force }
    Fail "не удалось скачать WebView2 bootstrapper с $Url : $($_.Exception.Message). Проверьте подключение к интернету и повторите сборку."
}

$size = (Get-Item $tmp).Length
if ($size -lt 500KB -or $size -gt 20MB) {
    Remove-Item $tmp -Force
    Fail "скачанный файл имеет неожиданный размер ($size байт) — это не bootstrapper WebView2."
}
$check = Test-MicrosoftSignature $tmp
if (-not $check.ok) {
    Remove-Item $tmp -Force
    Fail "подпись скачанного bootstrapper не подтверждена: статус $($check.status), подписант '$($check.subject)'. $($check.message)"
}
Move-Item -Force $tmp $Dest
$hash = (Get-FileHash $Dest -Algorithm SHA256).Hash.ToLower()
Write-Host "Готово: $Dest ($size байт, подпись Microsoft действительна, SHA-256 $hash — для отчёта, не закреплён)"
exit 0
