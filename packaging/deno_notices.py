"""
Перечень Rust-компонентов, скомпонованных в deno.exe закреплённой версии, и тексты их лицензий (S4).

Инструмент разработчика, а не шаг сборки: результат `licenses/deno/THIRD_PARTY_RUST.txt` хранится в git
и попадает в сборку вместе с остальной папкой licenses/. Перезапускается только при смене версии Deno
в packaging/runtime/deno.lock (отдельным коммитом, вместе с обновлением Deno).

    git clone --depth 1 --branch v<версия> https://github.com/denoland/deno.git <папка>
    python packaging/deno_notices.py --deno-src <папка> [--cargo-home <кэш>] [--spdx-cache <папка>]

Нужны cargo (любая стабильная версия; файл rust-toolchain.toml Deno обходится RUSTUP_TOOLCHAIN=stable)
и доступ к crates.io (cargo скачивает исходники крейтов, чтобы прочитать их файлы лицензий) и GitHub
(тексты SPDX для крейтов без файла лицензии: github.com/spdx/license-list-data).

Метод. Состав определяется так же, как его определяет релизная сборка Deno (ci.generated.yml, «Build release»):
  cargo build --release --locked -p deno -p denort -p test_server --bin deno ... --features=deno/panic-trace
Набор функций (features) у этих пакетов объединяется, поэтому дерево строится для тех же трёх пакетов,
а в перечень берётся только поддерево пакета deno:
  cargo tree --locked -p deno -p denort -p test_server --features deno/panic-trace
             --target x86_64-pc-windows-msvc -e normal,no-proc-macro
-e normal,no-proc-macro: только зависимости, которые компонуются в исполняемый файл (без build- и dev-
зависимостей и процедурных макросов, работающих только при компиляции).

Файлы лицензий берутся из самих пакетов (LICENSE*, LICENCE*, COPYING*, NOTICE*, COPYRIGHT*, UNLICENSE*,
в том числе в подпапках LICENSES/ и licenses/). Одинаковые тексты печатаются один раз со списком пакетов.
Если в пакете нет ни одного такого файла, приводится стандартный текст SPDX для каждой лицензии из его
выражения — это помечено отдельно.

Компоненты C/C++ внутри статической библиотеки V8 (крейт v8) в Cargo не видны. Их состав (V8_COMPONENTS
ниже) установлен по исходникам крейта v8 той версии, что в Cargo.lock Deno: цели v8/BUILD.gn, значения
по умолчанию v8/gni/v8.gni, аргументы GN в build.rs и .gn крейта, включённые Deno функции крейта
(use_custom_libcxx, simdutf). По самому бинарнику deno.exe состав не сверялся. Тексты лицензий берутся
из репозитория V8 на теге версии V8 либо из upstream-репозиториев компонентов (адреса — в файле).
Результат: licenses/deno/V8_COMPONENTS.txt.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "packaging" / "runtime" / "deno.lock"
OUTPUT = ROOT / "licenses" / "deno" / "THIRD_PARTY_RUST.txt"
OUTPUT_V8 = ROOT / "licenses" / "deno" / "V8_COMPONENTS.txt"
TARGET = "x86_64-pc-windows-msvc"
RELEASE_PACKAGES = ["deno", "denort", "test_server"]
RELEASE_FEATURES = "deno/panic-trace"
LICENSE_FILE = re.compile(r"^(licen[cs]e|copying|notice|copyright|unlicense)([-_.].*)?$", re.I)
LICENSE_DIRS = ("LICENSES", "licenses", "LICENSE")
SPDX_URL = "https://raw.githubusercontent.com/spdx/license-list-data/main/text/{id}.txt"
SPDX_ALIASES = {"Apache-2.0 WITH LLVM-exception": ["Apache-2.0", "LLVM-exception"]}

V8_CRATE = ("v8", "150.4.0")                 # проверяется по Cargo.lock исходников Deno
V8_VERSION = "15.0.245.2"                    # v8/include/v8-version.h крейта; совпадает с `deno --version`
V8_RAW = f"https://raw.githubusercontent.com/v8/v8/{V8_VERSION}/"
LLVM_LICENSE = "https://raw.githubusercontent.com/llvm/llvm-project/main/libcxx/LICENSE.TXT"
# (компонент, входит ли в deno.exe, основание по исходникам, лицензия, исходный код, [адреса текстов лицензии])
V8_COMPONENTS = [
    (f"V8 {V8_VERSION}", "да", "движок JavaScript (крейт v8 — привязки к нему)", "BSD-3-Clause",
     f"https://github.com/v8/v8/tree/{V8_VERSION}",
     [V8_RAW + "LICENSE", V8_RAW + "LICENSE.v8", V8_RAW + "LICENSE.fdlibm", V8_RAW + "LICENSE.strongtalk"]),
    ("ICU 77.1 (код и данные)", "да",
     "v8_enable_i18n_support включён по умолчанию; .gn крейта: icu_use_data_file = false (данные встроены)",
     "Unicode-3.0 и лицензии, перечисленные в тексте", "https://github.com/unicode-org/icu/tree/release-77-1",
     ["https://raw.githubusercontent.com/unicode-org/icu/release-77-1/LICENSE"]),
    ("libc++ и libc++abi (LLVM)", "да",
     "функция use_custom_libcxx крейта v8 включена (features по умолчанию; в Cargo.lock Deno: simdutf, use_custom_libcxx)",
     "Apache-2.0 WITH LLVM-exception", "https://github.com/llvm/llvm-project", [LLVM_LICENSE]),
    ("glibc: sin/cos (IBM Accurate Mathematical Library)", "да",
     "v8/gni/v8.gni: v8_use_libm_trig_functions = is_clang; build.rs собирает clang (DISABLE_CLANG не задан); "
     "v8/BUILD.gn: цель :libm из third_party/glibc/src/sysdeps/ieee754/dbl-64 (s_sin.c, branred.c, ...)",
     "LGPL-2.1-or-later (заголовки файлов)", f"https://github.com/v8/v8/tree/{V8_VERSION}/third_party/glibc",
     [V8_RAW + "third_party/glibc/LICENSE"]),
    ("Abseil", "да", "v8/BUILD.gn: группа v8_abseil (//third_party/abseil-cpp:absl)", "Apache-2.0",
     "https://github.com/abseil/abseil-cpp", ["https://raw.githubusercontent.com/abseil/abseil-cpp/master/LICENSE"]),
    ("Highway", "да", "v8/BUILD.gn: //third_party/highway:libhwy", "Apache-2.0 OR BSD-3-Clause",
     "https://github.com/google/highway", ["https://raw.githubusercontent.com/google/highway/master/LICENSE"]),
    ("FP16", "да", "v8/BUILD.gn: //third_party/fp16", "MIT", "https://github.com/Maratyszcza/FP16",
     ["https://raw.githubusercontent.com/Maratyszcza/FP16/master/LICENSE"]),
    ("fast_float", "да", "v8/BUILD.gn: //third_party/fast_float", "Apache-2.0 OR MIT OR BSL-1.0 (здесь — MIT)",
     "https://github.com/fastfloat/fast_float",
     ["https://raw.githubusercontent.com/fastfloat/fast_float/main/LICENSE-MIT"]),
    ("Dragonbox", "да", "v8/BUILD.gn: //third_party/dragonbox", "Apache-2.0 WITH LLVM-exception OR BSL-1.0 (здесь — BSL-1.0)",
     "https://github.com/jk-jeon/dragonbox",
     ["https://raw.githubusercontent.com/jk-jeon/dragonbox/master/LICENSE-Boost"]),
    ("simdutf", "да", "функция simdutf крейта v8 (Deno: features = [\"simdutf\"]) -> rusty_v8_enable_simdutf=true",
     "Apache-2.0 OR MIT (здесь — MIT)", "https://github.com/simdutf/simdutf",
     ["https://raw.githubusercontent.com/simdutf/simdutf/master/LICENSE-MIT"]),
    ("inspector_protocol (crdtp)", "да", "v8/src/inspector/BUILD.gn (инспектор V8 используется deno --inspect)",
     "BSD-3-Clause", f"https://github.com/v8/v8/tree/{V8_VERSION}/third_party/inspector_protocol",
     [V8_RAW + "third_party/inspector_protocol/LICENSE"]),
    ("SipHash", "да", "v8/BUILD.gn: third_party/siphash", "CC0-1.0",
     f"https://github.com/v8/v8/tree/{V8_VERSION}/third_party/siphash", [V8_RAW + "third_party/siphash/LICENSE"]),
    ("rapidhash", "да", "v8/BUILD.gn: third_party/rapidhash-v8", "BSD-2-Clause",
     f"https://github.com/v8/v8/tree/{V8_VERSION}/third_party/rapidhash-v8",
     [V8_RAW + "third_party/rapidhash-v8/LICENSE"]),
    ("UTF-8 decoder (Bjoern Hoehrmann)", "да", "v8/BUILD.gn: third_party/utf8-decoder", "MIT",
     f"https://github.com/v8/v8/tree/{V8_VERSION}/third_party/utf8-decoder",
     [V8_RAW + "third_party/utf8-decoder/LICENSE"]),
    ("LLVM libc (заголовки)", "заголовки", "v8/BUILD.gn: //third_party/llvm-libc:headers",
     "Apache-2.0 WITH LLVM-exception", "https://github.com/llvm/llvm-project", [LLVM_LICENSE]),
    ("PartitionAlloc", "нет", "v8_enable_partition_alloc требует pointer compression; build.rs: "
     "v8_enable_pointer_compression=false без одноимённой функции крейта, Deno её не включает", "BSD-3-Clause", "", []),
    ("wasm C API (third_party/wasm-api)", "нет (по исходникам)", "только заголовки цели v8_libwasm_c_api, "
     "которую Deno не использует", "Apache-2.0", "", []),
    ("libunwind, googletest, jinja2, markupsafe, re2, jsoncpp, vtune, valgrind", "нет",
     "инструменты сборки и тестов либо опции, выключенные по умолчанию; libunwind не используется на Windows (MSVC)",
     "разные", "", []),
]


def cargo(args: list[str], src: Path, cargo_home: Path | None) -> str:
    env = {**os.environ, "RUSTUP_TOOLCHAIN": os.environ.get("RUSTUP_TOOLCHAIN", "stable")}
    if cargo_home:
        env["CARGO_HOME"] = str(cargo_home)
    p = subprocess.run(["cargo", *args], cwd=src, env=env, capture_output=True, text=True, encoding="utf-8")
    if p.returncode != 0:
        sys.exit(f"cargo {' '.join(args)} завершился с кодом {p.returncode}:\n{p.stderr[-3000:]}")
    return p.stdout


def deno_subtree(tree: str) -> list[tuple[str, str, str, str]]:
    """Строки `глубина|пакет|лицензия|репозиторий` -> (имя, версия, лицензия, репозиторий) из поддерева deno."""
    out, inside = {}, False
    for line in tree.splitlines():
        line = line.replace(" (*)", "")                    # повтор уже показанного пакета
        depth, _, rest = line.partition("|")
        pkg, lic, repo = (rest.split("|") + ["", ""])[:3]
        if depth == "0":
            inside = pkg.startswith("deno v")
        if not inside:
            continue
        m = re.match(r"(\S+) v(\S+)", pkg)
        if m:
            out[(m.group(1), m.group(2))] = (m.group(1), m.group(2), lic.strip(), repo.strip())
    return sorted(out.values(), key=lambda x: (x[0].lower(), x[1]))


def manifest_dirs(metadata: dict) -> dict[tuple[str, str], Path]:
    return {(p["name"], p["version"]): Path(p["manifest_path"]).parent for p in metadata["packages"]}


def authors(metadata: dict) -> dict[tuple[str, str], list[str]]:
    return {(p["name"], p["version"]): p.get("authors") or [] for p in metadata["packages"]}


def license_files(pkg_dir: Path) -> list[Path]:
    found = [f for f in sorted(pkg_dir.iterdir()) if f.is_file() and LICENSE_FILE.match(f.name)]
    for d in LICENSE_DIRS:
        sub = pkg_dir / d
        if sub.is_dir():
            found += [f for f in sorted(sub.rglob("*")) if f.is_file()]
    return found


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in text.split("\n")).strip("\n") + "\n"


def spdx_ids(expression: str) -> list[str]:
    expr = expression.replace("/", " OR ")
    ids: list[str] = []
    for alias, parts in SPDX_ALIASES.items():
        if alias in expr:
            ids += parts
            expr = expr.replace(alias, " ")
    ids += [t for t in re.findall(r"[A-Za-z0-9.\-+]+", expr) if t not in ("OR", "AND", "WITH")]
    return list(dict.fromkeys(i.rstrip("+") for i in ids))


def spdx_text(spdx_id: str, cache: Path) -> str:
    cache.mkdir(parents=True, exist_ok=True)
    f = cache / f"{spdx_id}.txt"
    if not f.exists():
        with urllib.request.urlopen(SPDX_URL.format(id=spdx_id), timeout=60) as r:
            f.write_bytes(r.read())
    return read_text(f)


def fetch_text(url: str, cache: Path) -> str:
    cache.mkdir(parents=True, exist_ok=True)
    f = cache / (hashlib.sha256(url.encode()).hexdigest()[:16] + ".txt")
    if not f.exists():
        with urllib.request.urlopen(url, timeout=60) as r:
            f.write_bytes(r.read())
    return read_text(f)


def write_v8_components(path: Path, deno_version: str, cache: Path) -> None:
    lines = [
        f"Deno {deno_version}: компоненты C/C++ в статической библиотеке V8 (крейт {V8_CRATE[0]} {V8_CRATE[1]})",
        "=" * 78,
        "",
        f"Сформировано packaging/deno_notices.py {dt.date.today().isoformat()}. Состав установлен по исходникам",
        f"крейта {V8_CRATE[0]} {V8_CRATE[1]} (как его использует Deno {deno_version}): цели v8/BUILD.gn, значения по",
        "умолчанию v8/gni/v8.gni, аргументы GN в build.rs и .gn крейта, включённые функции крейта. Deno",
        "компонует готовую статическую библиотеку rusty_v8 для Windows; по самому бинарнику deno.exe состав",
        "НЕ сверялся. Тексты лицензий — из репозитория V8 на теге версии V8 или из upstream-репозиториев",
        "компонентов (адрес у каждого текста); ревизии компонентов в V8 закреплены в файле DEPS V8.",
        "",
        "ВНИМАНИЕ: по исходникам в V8 встроены функции sin/cos из glibc (LGPL-2.1-or-later). Исходный код",
        f"этих файлов: https://github.com/v8/v8/tree/{V8_VERSION}/third_party/glibc ; исходный код Deno и V8",
        "открыт, deno.exe можно пересобрать с изменённой версией этих функций. Текст LGPL-2.1 — ниже.",
        "",
        "1. Компоненты",
        "-" * 78,
    ]
    for name, inc, why, lic, src, _urls in V8_COMPONENTS:
        lines += [f"* {name}", f"    входит в deno.exe (по исходникам): {inc}", f"    основание: {why}", f"    лицензия: {lic}"]
        if src:
            lines.append(f"    исходный код: {src}")
    lines += ["", "", "2. Тексты лицензий", "-" * 78]
    seen: dict[str, str] = {}
    for name, _inc, _why, _lic, _src, urls in V8_COMPONENTS:
        for url in urls:
            if url in seen:
                lines += ["", f"[{name}: текст тот же, что для «{seen[url]}» выше — {url}]"]
                continue
            seen[url] = name
            lines += ["", "~" * 78, f"{name} — {url}", "~" * 78, fetch_text(url, cache).rstrip(), ""]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"{path}: {len(V8_COMPONENTS)} компонентов, {len(seen)} текстов")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Rust-компоненты deno.exe и их лицензии")
    ap.add_argument("--deno-src", required=True, type=Path, help="исходники Deno на теге закреплённой версии")
    ap.add_argument("--cargo-home", type=Path, help="CARGO_HOME (кэш скачанных крейтов)")
    ap.add_argument("--spdx-cache", type=Path, default=Path("build") / "spdx", help="кэш текстов SPDX")
    ap.add_argument("--output", type=Path, default=OUTPUT)
    args = ap.parse_args(argv)

    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    version = lock["version"]
    src_version = re.search(r'^version = "([^"]+)"', (args.deno_src / "cli" / "Cargo.toml").read_text(encoding="utf-8"), re.M)
    if not src_version or src_version.group(1) != version:
        sys.exit(f"исходники Deno {src_version and src_version.group(1)} не совпадают с deno.lock {version}")

    pkgs = sum((["-p", p] for p in RELEASE_PACKAGES), [])
    tree = cargo(["tree", "--locked", *pkgs, "--features", RELEASE_FEATURES, "--target", TARGET,
                  "-e", "normal,no-proc-macro", "--prefix", "depth", "--format", "|{p}|{l}|{r}"],
                 args.deno_src, args.cargo_home)
    crates = deno_subtree(tree)
    # только для путей к пакетам: --all-features, чтобы в списке были и необязательные зависимости
    meta = json.loads(cargo(["metadata", "--format-version", "1", "--locked", "--all-features", "--filter-platform", TARGET],
                            args.deno_src, args.cargo_home))
    if not any((p["name"], p["version"]) == V8_CRATE for p in meta["packages"]):
        sys.exit(f"в Cargo.lock Deno нет крейта {V8_CRATE[0]} {V8_CRATE[1]}: обновите V8_COMPONENTS по его исходникам")
    dirs = manifest_dirs(meta)
    who = authors(meta)
    deno_root_license = args.deno_src / "LICENSE.md"

    texts: dict[str, tuple[str, list[str]]] = {}         # sha256 -> (текст, [пакеты])
    no_file: list[tuple[str, str, list[str]]] = []        # (пакет, выражение лицензии, авторы из Cargo.toml)
    for name, ver, lic, _repo in crates:
        label = f"{name} {ver}"
        pkg_dir = dirs.get((name, ver))
        if pkg_dir is None:
            sys.exit(f"нет пакета {label} в cargo metadata")
        files = license_files(pkg_dir)
        if not files and pkg_dir.is_relative_to(args.deno_src.resolve()):
            files = [deno_root_license]                    # крейты рабочего пространства Deno: общая лицензия
        if not files:
            no_file.append((label, lic, who.get((name, ver), [])))
            continue
        for f in files:
            body = read_text(f)
            key = hashlib.sha256(body.encode()).hexdigest()
            texts.setdefault(key, (body, []))[1].append(f"{label} ({f.name})")

    spdx_needed: dict[str, list[str]] = defaultdict(list)
    for label, lic, _who in no_file:
        ids = spdx_ids(lic) if lic else []
        if not ids:
            sys.exit(f"у {label} нет ни файла лицензии, ни выражения license — нужна ручная проверка")
        for i in ids:
            spdx_needed[i].append(label)

    licenses = defaultdict(int)
    for _n, _v, lic, _r in crates:
        licenses[lic or "(не указана)"] += 1
    w = max(len(f"{n} {v}") for n, v, _l, _r in crates)
    lines = [
        f"Deno {version} (deno.exe, {TARGET}): Rust-компоненты и их лицензии",
        "=" * 78,
        "",
        f"Сформировано packaging/deno_notices.py {dt.date.today().isoformat()} из исходников тега v{version}",
        f"(https://github.com/denoland/deno/tree/v{version}). Метод и ограничения — в описании этого скрипта:",
        f"  cargo tree --locked {' '.join(pkgs)} --features {RELEASE_FEATURES} --target {TARGET}",
        "             -e normal,no-proc-macro   (берётся поддерево пакета deno)",
        "так же, как собирает релиз Deno; процедурные макросы, build- и dev-зависимости в бинарник не входят.",
        "Компоненты C/C++ внутри библиотеки V8 (крейт v8): licenses\\deno\\V8_COMPONENTS.txt.",
        "",
        f"Пакетов: {len(crates)}. Лицензии (как указаны в Cargo.toml пакетов):",
        *[f"  {n:4d}  {lic}" for lic, n in sorted(licenses.items(), key=lambda x: (-x[1], x[0]))],
        "",
        "Пакеты с выражением «A OR B» используются Deno на условиях любой из перечисленных лицензий; ниже",
        "приведены все тексты, найденные в пакетах.",
        "",
        "",
        "1. Перечень пакетов",
        "-" * 78,
        *[f"{(n + ' ' + v).ljust(w)}  {lic or '(не указана в Cargo.toml)'}  {repo}".rstrip() for n, v, lic, repo in crates],
        "",
        "",
        "2. Тексты лицензий и уведомлений из пакетов (одинаковые тексты — один раз)",
        "-" * 78,
    ]
    for body, users in sorted(texts.values(), key=lambda t: t[1][0].lower()):
        lines += ["", "~" * 78, "Применяется к: " + "; ".join(users), "~" * 78, body.rstrip(), ""]
    lines += ["", "3. Пакеты без файла лицензии: правообладатели и стандартные тексты SPDX", "-" * 78,
              "В этих пакетах на crates.io нет файла лицензии. Правообладатели — по полю authors в Cargo.toml",
              "пакета (если оно заполнено); исходный код: https://crates.io/crates/<имя>/<версия>.", ""]
    if not spdx_needed:
        lines.append("Нет: у каждого пакета есть собственный файл лицензии.")
    for label, lic, names in no_file:
        lines.append(f"{label}: {lic}; авторы: {', '.join(names) if names else 'не указаны'}")
    for spdx_id, users in sorted(spdx_needed.items()):
        lines += ["", "~" * 78, f"{spdx_id} (SPDX) — для: " + "; ".join(sorted(set(users))), "~" * 78,
                  spdx_text(spdx_id, args.spdx_cache).rstrip(), ""]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    write_v8_components(args.output.parent / OUTPUT_V8.name, version, args.spdx_cache)
    print(f"{args.output}: {len(crates)} пакетов, {len(texts)} уникальных текстов, "
          f"без файла лицензии: {len(no_file)} ({', '.join(n[0] for n in no_file) or '—'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
