; Установщик Wave / «ВОЛНА» для Windows x64 (Inno Setup 6, проверено с 6.7.3). Этап S4.
;
; Сборка — только через build_windows.bat (или CI), который передаёт версию:
;   ISCC.exe /DAppVersion=<версия из backend\version.py> installer\wave.iss
; Результат: dist\installer\Wave-Setup-<версия>.exe.
;
; Что делает установщик:
;   * ставит настоящую собранную папку dist\Wave целиком (Wave.exe, _internal с Python, frontend,
;     runtime\deno, licenses) в %LOCALAPPDATA%\Programs\Wave — для текущего пользователя, без прав
;     администратора;
;   * ярлык в меню «Пуск», по желанию — на рабочем столе;
;   * проверяет Microsoft Edge WebView2 Runtime по тем же ключам реестра, что backend\launcher.py
;     (webview2_installed, WEBVIEW2_KEYS); если его нет — запускает официальный bootstrapper Microsoft
;     тихо и ждёт его, затем проверяет реестр снова;
;   * обновление поверх: тот же AppId, запущенную ВОЛНУ находит по mutex приложения (AppMutex) и просит
;     закрыть; файлы программы заменяются, старая папка _internal удаляется перед копированием;
;   * удаление убирает только файлы программы и ярлыки. Данные пользователя (%LOCALAPPDATA%\Wave:
;     база, настройки, история, журналы) и папка загрузок НЕ удаляются — установщик их не создаёт
;     и не знает о них.
;
; Подписи кода нет (ADR-8). Необязательная подпись: build_windows.bat передаёт /DSignTool=... и /S...,
; только если задана переменная WAVE_SIGN_COMMAND (см. README); без неё этот блок не компилируется.
;
; Тестовый вариант (только CI, tests/installer): /DTestWebView2=ok|fail — WebView2 считается
; отсутствующим, вместо bootstrapper выполняется «cmd /c exit 0|1603», после него WebView2 считается
; установленным только для ok. В выпускаемый установщик этот код не попадает (директива не задана).

#ifndef AppVersion
  #error Не передана версия: ISCC /DAppVersion=<версия> (её печатает python backend\version.py). Запускайте build_windows.bat.
#endif

#define DistDir AddBackslash(SourcePath) + "..\dist\Wave"
#define AppExe DistDir + "\Wave.exe"
#ifndef WebView2Bootstrapper
  #define WebView2Bootstrapper AddBackslash(SourcePath) + "..\build\webview2\MicrosoftEdgeWebview2Setup.exe"
#endif
#ifndef OutputDir
  #define OutputDir AddBackslash(SourcePath) + "..\dist\installer"
#endif

#if !FileExists(AppExe)
  #error Не найдена сборка dist\Wave\Wave.exe. Сначала соберите её (build_windows.bat).
#endif
#if !DirExists(DistDir + "\_internal\runtime\deno")
  #error В dist\Wave нет _internal\runtime\deno: сборка неполная (packaging\fetch_runtime.py, wave.spec).
#endif
#if !FileExists(DistDir + "\_internal\licenses\THIRD_PARTY_NOTICES.txt")
  #error В dist\Wave нет _internal\licenses\THIRD_PARTY_NOTICES.txt.
#endif
#ifndef TestWebView2
  #if !FileExists(WebView2Bootstrapper)
    #error Нет MicrosoftEdgeWebview2Setup.exe (build\webview2): его скачивает и проверяет packaging\fetch_webview2.ps1.
  #endif
#endif

; Версия, название, издатель и описание — из version-resource собранного Wave.exe (его пишет wave.spec
; из backend\version.py). /DAppVersion обязан совпадать с версией exe.
#define ExeVersion GetVersionNumbersString(AppExe)
#if ExeVersion != AppVersion + ".0"
  #error Версия в Wave.exe не совпадает с /DAppVersion (build_windows.bat сверяет их до компиляции и печатает обе). Пересоберите dist\Wave.
#endif
#define AppName GetStringFileInfo(AppExe, "ProductName")
#define AppPublisher GetStringFileInfo(AppExe, "CompanyName")
#define AppDescription GetStringFileInfo(AppExe, "FileDescription")
#if AppName == "" || AppPublisher == ""
  #error В version-resource Wave.exe нет ProductName/CompanyName.
#endif

[Setup]
; AppId не меняется никогда: по нему обновление находит предыдущую установку.
AppId={{314C4533-1390-4F2A-B0B8-7003E0F6F200}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL=https://github.com/Andrey3269/Wave
AppSupportURL=https://github.com/Andrey3269/Wave
AppComments={#AppDescription}
VersionInfoVersion={#AppVersion}.0
VersionInfoProductVersion={#AppVersion}
VersionInfoProductName={#AppName}
VersionInfoCompany={#AppPublisher}
VersionInfoDescription=Установщик: {#AppDescription}

; Для текущего пользователя, без прав администратора и без запроса повышения
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Wave
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

; Запущенная ВОЛНА: mutex из backend\launcher.py single_instance(). Без префикса и с Local\ это один
; объект пространства имён сеанса; установщик проверяет его при запуске и перед удалением.
AppMutex=Local\Wave.Desktop.SingleInstance
; Один установщик ВОЛНЫ за раз
SetupMutex=Local\Wave.Desktop.Setup
; Если ВОЛНА всё же держит файлы (пользователь нажал OK, не закрыв её) — Restart Manager предложит
; закрыть её; перезапускать её Restart Manager'ом не нужно: на последней странице есть «Запустить».
CloseApplications=yes
RestartApplications=no

OutputDir={#OutputDir}
OutputBaseFilename=Wave-Setup-{#AppVersion}
SetupIconFile=..\packaging\wave.ico
UninstallDisplayIcon={app}\Wave.exe
UninstallDisplayName={#AppName}
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ShowLanguageDialog=auto
#ifdef SignTool
SignTool={#SignTool}
SignedUninstaller=yes
#endif

[Languages]
Name: "ru"; MessagesFile: "compiler:Languages\Russian.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[CustomMessages]
ru.LaunchApp=Запустить ВОЛНУ
en.LaunchApp=Launch Wave
ru.WebView2Installing=Устанавливается Microsoft Edge WebView2 Runtime (нужен для окна ВОЛНЫ)…
en.WebView2Installing=Installing Microsoft Edge WebView2 Runtime (required for the Wave window)…
ru.WebView2Failed=ВОЛНА установлена, но не удалось установить Microsoft Edge WebView2 Runtime (код %1). Без него окно приложения не откроется.%n%nЧто сделать:%n1. Проверьте подключение к интернету и запустите установщик ВОЛНЫ ещё раз.%n2. Или установите WebView2 Runtime вручную со страницы Microsoft:%n   https://developer.microsoft.com/microsoft-edge/webview2/consumer/%n3. Если компьютер управляется организацией, обратитесь к администратору.%n%nПосле установки WebView2 запустите ВОЛНУ из меню «Пуск».
en.WebView2Failed=Wave is installed, but Microsoft Edge WebView2 Runtime could not be installed (code %1). The Wave window cannot open without it.%n%nWhat to do:%n1. Check your internet connection and run the Wave installer again.%n2. Or install WebView2 Runtime from Microsoft:%n   https://developer.microsoft.com/microsoft-edge/webview2/consumer/%n3. On a managed computer, contact your administrator.%n%nAfter installing WebView2, start Wave from the Start menu.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[InstallDelete]
; Обновление: файлы прошлой сборки PyInstaller в _internal не должны смешиваться с новыми.
; Удаляется только каталог программы; данные пользователя лежат в %LOCALAPPDATA%\Wave отдельно.
Type: filesandordirs; Name: "{app}\_internal"

[Files]
; Вся собранная папка dist\Wave, рекурсивно (включая _internal, runtime\deno и licenses); Wave.exe —
; отдельной строкой, чтобы при заданной подписи (SignTool) подписать его флагом sign
Source: "{#DistDir}\*"; Excludes: "\Wave.exe"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
#ifdef SignTool
Source: "{#DistDir}\Wave.exe"; DestDir: "{app}"; Flags: ignoreversion sign
#else
Source: "{#DistDir}\Wave.exe"; DestDir: "{app}"; Flags: ignoreversion
#endif
; Уведомления о сторонних компонентах — и рядом с Wave.exe, чтобы их было легко найти
Source: "{#DistDir}\_internal\licenses\THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
#ifndef TestWebView2
; Официальный bootstrapper WebView2: распаковывается во временную папку, только если runtime нет
Source: "{#WebView2Bootstrapper}"; DestName: "MicrosoftEdgeWebview2Setup.exe"; Flags: dontcopy
#endif

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\Wave.exe"; Comment: "{#AppDescription}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\Wave.exe"; Comment: "{#AppDescription}"; Tasks: desktopicon

[Run]
; Запуск предлагается только при подтверждённом WebView2 (иначе окно не откроется)
Filename: "{app}\Wave.exe"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent; Check: WebView2Ready

[Code]
const
  WebView2Guid = '{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  { Код выхода установщика, если программа установлена, а WebView2 — нет (коды 1–8 заняты Inno Setup) }
  ExitCodeWebView2Missing = 100;

var
  WebView2Ok: Boolean;
  WebView2Checked: Boolean;

{ Те же места, что backend\launcher.py WEBVIEW2_KEYS (64-битное представление реестра):
  HKLM\SOFTWARE\WOW6432Node\..., HKLM\SOFTWARE\..., HKCU\Software\...; значение pv не пустое и не 0.0.0.0.
  Согласованность списков проверяет tests/unit/test_installer_script.py. }
function WebView2VersionAt(RootKey: Integer; SubKey: String): String;
var
  Version: String;
begin
  Result := '';
  if RegQueryStringValue(RootKey, SubKey, 'pv', Version) then
    if (Version <> '') and (Version <> '0.0.0.0') then
      Result := Version;
end;

function DetectWebView2(): Boolean;
var
  Version: String;
begin
#ifdef TestWebView2
  Log('Wave: TEST MODE TestWebView2={#TestWebView2}: WebView2 is treated as missing');
  Result := False;
  exit;
#endif
  Version := WebView2VersionAt(HKLM64, 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\' + WebView2Guid);
  if Version = '' then
    Version := WebView2VersionAt(HKLM64, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\' + WebView2Guid);
  if Version = '' then
    Version := WebView2VersionAt(HKCU, 'Software\Microsoft\EdgeUpdate\Clients\' + WebView2Guid);
  Result := Version <> '';
  if Result then
    Log('Wave: WebView2 Runtime found, pv=' + Version)
  else
    Log('Wave: WebView2 Runtime not found');
end;

{ Запускает bootstrapper тихо и ждёт завершения. Возвращает код выхода (-1 — не удалось запустить). }
function RunWebView2Bootstrapper(): Integer;
var
  ResultCode: Integer;
  Path: String;
begin
  Result := -1;
#ifdef TestWebView2
  #if TestWebView2 == "ok"
  Path := '/c exit 0';
  #else
  Path := '/c exit 1603';
  #endif
  if Exec(ExpandConstant('{cmd}'), Path, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Result := ResultCode;
  Log('Wave: TEST MODE fake bootstrapper exit code ' + IntToStr(Result));
  exit;
#else
  ExtractTemporaryFile('MicrosoftEdgeWebview2Setup.exe');
  Path := ExpandConstant('{tmp}\MicrosoftEdgeWebview2Setup.exe');
  Log('Wave: running WebView2 bootstrapper: ' + Path + ' /silent /install');
  { Без прав администратора bootstrapper ставит WebView2 для текущего пользователя (документация Microsoft) }
  if Exec(Path, '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Result := ResultCode
  else
    Log('Wave: WebView2 bootstrapper could not be started: ' + SysErrorMessage(ResultCode));
  Log('Wave: WebView2 bootstrapper exit code ' + IntToStr(Result));
#endif
end;

procedure EnsureWebView2();
var
  Code: Integer;
begin
  WebView2Ok := DetectWebView2();
  if not WebView2Ok then
  begin
    WizardForm.StatusLabel.Caption := CustomMessage('WebView2Installing');
    Code := RunWebView2Bootstrapper();
    { Итог решает реестр, а не только код выхода: успех без записи в реестре — не успех }
#ifdef TestWebView2
  #if TestWebView2 == "ok"
    WebView2Ok := Code = 0;
  #else
    WebView2Ok := False;
  #endif
#else
    WebView2Ok := DetectWebView2();
#endif
    if WebView2Ok then
      Log('Wave: WebView2 Runtime installed')
    else
    begin
      Log('Wave: WebView2 Runtime is still missing after the bootstrapper (code ' + IntToStr(Code) + ')');
      SuppressibleMsgBox(FmtMessage(CustomMessage('WebView2Failed'), [IntToStr(Code)]), mbError, MB_OK, IDOK);
    end;
  end;
  WebView2Checked := True;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    EnsureWebView2();
end;

{ Для [Run]: предлагать запуск ВОЛНЫ только с подтверждённым WebView2 }
function WebView2Ready(): Boolean;
begin
  Result := WebView2Checked and WebView2Ok;
  if WebView2Checked and not WebView2Ok then
    Log('Wave: launch after install is not offered: WebView2 Runtime is missing');
end;

{ Тихая установка: ненулевой код, если программа установлена, но WebView2 нет }
function GetCustomSetupExitCode(): Integer;
begin
  Result := 0;
  if WebView2Checked and not WebView2Ok then
    Result := ExitCodeWebView2Missing;
end;
