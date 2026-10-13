"""
Установщик installer/wave.iss (S4) и его согласованность с приложением — без Windows и без Inno Setup.

Компиляция, установка, обновление и удаление проверяются в CI на windows-latest (job installer,
tests/installer/run_installer_checks.ps1). Здесь — то, что легко сломать правкой текста и трудно заметить:
фиксированный AppId, установка без прав администратора, те же ключи реестра WebView2 и тот же mutex,
что в backend/launcher.py, полная папка dist\\Wave, отсутствие удаления данных пользователя.
"""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

import launcher

ROOT = Path(__file__).resolve().parents[2]
ISS = ROOT / "installer" / "wave.iss"
APP_ID = "314C4533-1390-4F2A-B0B8-7003E0F6F200"     # не меняется никогда: по нему находится прошлая версия


@pytest.fixture(scope="module")
def iss() -> str:
    raw = ISS.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "wave.iss — UTF-8 с BOM (иначе Inno Setup прочтёт кириллицу неверно)"
    return raw.decode("utf-8-sig")


def _section(text: str, name: str) -> str:
    m = re.search(rf"^\[{re.escape(name)}\]\s*$(.*?)(?=^\[[A-Za-z]+\]\s*$|\Z)", text, re.M | re.S)
    assert m, f"нет секции [{name}]"
    return m.group(1)


def _directives(text: str) -> dict[str, str]:
    out = {}
    for line in _section(text, "Setup").splitlines():
        line = line.strip()
        if line and not line.startswith((";", "#")) and "=" in line:
            k, v = line.split("=", 1)
            out.setdefault(k.strip(), v.strip())
    return out


def test_fixed_app_id(iss):
    assert _directives(iss)["AppId"] == "{{" + APP_ID + "}"


def test_per_user_install_without_admin(iss):
    d = _directives(iss)
    assert d["PrivilegesRequired"] == "lowest"
    assert "PrivilegesRequiredOverridesAllowed" not in d          # нельзя переключить на установку для всех
    assert d["DefaultDirName"] == r"{localappdata}\Programs\Wave"
    assert d["ArchitecturesAllowed"] == "x64compatible" and d["ArchitecturesInstallIn64BitMode"] == "x64compatible"


def test_app_mutex_matches_launcher(iss):
    assert _directives(iss)["AppMutex"] == launcher.SINGLE_INSTANCE_MUTEX
    assert launcher.SINGLE_INSTANCE_MUTEX == "Local\\Wave.Desktop.SingleInstance"


def test_version_comes_from_the_build(iss):
    d = _directives(iss)
    assert d["AppVersion"] == "{#AppVersion}" and d["OutputBaseFilename"] == "Wave-Setup-{#AppVersion}"
    assert "#ifndef AppVersion" in iss and "#error" in iss
    assert 'GetVersionNumbersString(AppExe)' in iss and '#if ExeVersion != AppVersion + ".0"' in iss
    assert not re.search(r"^\s*#define\s+AppVersion\b", iss, re.M), "версию передаёт только build_windows.bat"


def test_whole_dist_folder_is_installed(iss):
    files = _section(iss, "Files")
    assert re.search(r'Source: "\{#DistDir\}\\\*"; Excludes: "\\Wave\.exe"; DestDir: "\{app\}"; '
                     r'Flags: ignoreversion recursesubdirs createallsubdirs', files)
    assert files.count('Source: "{#DistDir}\\Wave.exe"; DestDir: "{app}"') == 2   # с подписью и без
    assert r'#define DistDir AddBackslash(SourcePath) + "..\dist\Wave"' in iss
    for required in (r"\_internal\runtime\deno", r"\_internal\licenses\THIRD_PARTY_NOTICES.txt"):
        assert required in iss                                     # без них компиляция останавливается


def test_user_data_is_never_deleted(iss):
    """Ни установщик, ни деинсталлятор не трогают %LOCALAPPDATA%\\Wave и загрузки."""
    assert "[UninstallDelete]" not in iss
    deletes = _section(iss, "InstallDelete")
    targets = re.findall(r'Name: "([^"]+)"', deletes)
    assert targets == [r"{app}\_internal"]
    code = _section(iss, "Code")
    assert not re.search(r"DelTree|DeleteFile|RemoveDir", code)
    assert "{localappdata}\\Wave" not in iss.replace(r"{localappdata}\Programs\Wave", "")
    assert "Downloads" not in iss


def test_webview2_registry_keys_match_launcher(iss):
    code = _section(iss, "Code")
    guid = re.search(r"WebView2Guid = '(\{[0-9A-F-]+\})';", code).group(1)
    assert guid == launcher.WEBVIEW2_GUID
    found = re.findall(r"WebView2VersionAt\((HKLM64|HKCU), '([^']+)' \+ WebView2Guid\)", code)
    keys = [("HKLM" if hive == "HKLM64" else hive, path + guid) for hive, path in found]
    assert keys == list(launcher.WEBVIEW2_KEYS)
    assert "(Version <> '') and (Version <> '0.0.0.0')" in code


def test_webview2_bootstrapper_is_silent_waited_and_rechecked(iss):
    code = _section(iss, "Code")
    assert "Exec(Path, '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode)" in code
    assert "WebView2Ok := DetectWebView2();" in code.split("Code := RunWebView2Bootstrapper();", 1)[1]
    assert "SuppressibleMsgBox(FmtMessage(CustomMessage('WebView2Failed')" in code
    assert "ExitCodeWebView2Missing = 100" in code
    run = _section(iss, "Run")
    assert "Check: WebView2Ready" in run and "skipifsilent" in run


def test_test_mode_is_compiled_only_on_request(iss):
    """Тестовая подмена WebView2 существует только под #ifdef TestWebView2 (ISCC /DTestWebView2=...)."""
    assert not re.search(r"^\s*#define\s+TestWebView2\b", iss, re.M)
    stack: list[list] = []                  # [условие, ветка #else?] для вложенных #if/#ifdef/#ifndef
    test_lines = 0
    for line in iss.splitlines():
        d = line.strip()
        if d.startswith("#ifdef "):
            stack.append([d.split()[1], False])
        elif d.startswith(("#ifndef ", "#if ")):
            stack.append([d, False])
        elif d == "#else":
            stack[-1][1] = True
        elif d == "#endif":
            stack.pop()
        elif not d.startswith(";") and ("TEST MODE" in line or "/c exit" in line):
            test_lines += 1
            assert ["TestWebView2", False] in stack, f"тестовый код вне #ifdef TestWebView2: {line.strip()}"
    assert not stack and test_lines >= 4


def test_russian_messages_and_mandatory_texts(iss):
    msgs = _section(iss, "CustomMessages")
    for key in ("LaunchApp", "WebView2Installing", "WebView2Failed"):
        assert re.search(rf"^ru\.{key}=", msgs, re.M) and re.search(rf"^en\.{key}=", msgs, re.M)
    assert 'MessagesFile: "compiler:Languages\\Russian.isl"' in iss


# ---------------------------------------------------------------------------
# launcher.webview2_installed() — та же логика со стороны приложения (WIN-3, часть приложения)
# ---------------------------------------------------------------------------
@pytest.fixture
def fake_registry(monkeypatch):
    values: dict[tuple[str, str], str | None] = {}

    class Key:
        def __init__(self, hive, path):
            self.k = (hive, path)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def open_key(hive, path):
        if (hive, path) not in values:
            raise FileNotFoundError(path)
        return Key(hive, path)

    def query(key, name):
        assert name == "pv"
        v = values[key.k]
        if v is None:
            raise FileNotFoundError(name)
        return v, 1

    winreg = types.SimpleNamespace(HKEY_LOCAL_MACHINE="HKLM", HKEY_CURRENT_USER="HKCU",
                                   OpenKey=open_key, QueryValueEx=query)
    monkeypatch.setitem(sys.modules, "winreg", winreg)
    monkeypatch.setattr(launcher, "IS_WIN", True)
    return values


def test_webview2_missing_when_no_keys(fake_registry):
    assert launcher.webview2_installed() is False


@pytest.mark.parametrize("index", [0, 1, 2])
def test_webview2_found_in_any_location(fake_registry, index):
    fake_registry[launcher.WEBVIEW2_KEYS[index]] = "131.0.2903.70"
    assert launcher.webview2_installed() is True


@pytest.mark.parametrize("pv", ["", "0.0.0.0", None])
def test_webview2_placeholder_versions_do_not_count(fake_registry, pv):
    for key in launcher.WEBVIEW2_KEYS:
        fake_registry[key] = pv
    assert launcher.webview2_installed() is False


# ---------------------------------------------------------------------------
# PowerShell-скрипты сборки и проверок установщика: синтаксис (pwsh есть на раннерах GitHub)
# ---------------------------------------------------------------------------
PS_SCRIPTS = [ROOT / "packaging" / "fetch_webview2.ps1", ROOT / "tests" / "installer" / "run_installer_checks.ps1"]


@pytest.mark.parametrize("script", PS_SCRIPTS, ids=lambda p: p.name)
def test_powershell_script_has_bom_for_windows_powershell(script):
    assert script.read_bytes().startswith(b"\xef\xbb\xbf"), "без BOM Windows PowerShell 5.1 прочтёт кириллицу неверно"


@pytest.mark.parametrize("script", PS_SCRIPTS, ids=lambda p: p.name)
def test_powershell_script_parses(script):
    import shutil
    import subprocess

    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh не установлен (на раннерах GitHub он есть)")
    path = str(script).replace("'", "''")
    cmd = (f"$t=$null; $e=$null; [System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$t, [ref]$e) "
           "| Out-Null; $e | ForEach-Object { $_.Extent.StartLineNumber.ToString() + ': ' + $_.Message }; exit $e.Count")
    p = subprocess.run([pwsh, "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
