"""
Уведомления о сторонних компонентах (S4): licenses/ и генератор packaging/deno_notices.py.

Сам генератор запускается вручную при обновлении Deno (нужны исходники Deno, cargo и сеть); здесь
проверяются его разбор вывода cargo и то, что сохранённые в git перечни соответствуют закреплённому Deno.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "packaging"))
import deno_notices  # noqa: E402

LIC = ROOT / "licenses"


def test_cargo_tree_subtree_of_deno_only():
    tree = "\n".join([
        "0|deno v2.9.7 (/src/cli)|MIT|https://github.com/denoland/deno",
        "1|serde v1.0.200|MIT OR Apache-2.0|https://github.com/serde-rs/serde",
        "2|serde_derive v1.0.200|MIT OR Apache-2.0|https://github.com/serde-rs/serde (*)",
        "1|serde v1.0.200|MIT OR Apache-2.0|https://github.com/serde-rs/serde (*)",
        "0|denort v2.9.7 (/src/cli/rt)|MIT|https://github.com/denoland/deno",
        "1|only_in_denort v0.1.0|MIT|",
    ])
    crates = deno_notices.deno_subtree(tree)
    assert [(n, v) for n, v, _l, _r in crates] == [("deno", "2.9.7"), ("serde", "1.0.200"), ("serde_derive", "1.0.200")]
    assert crates[2][3] == "https://github.com/serde-rs/serde"            # без пометки повтора «(*)»


def test_spdx_ids_from_expressions():
    assert deno_notices.spdx_ids("MIT OR Apache-2.0") == ["MIT", "Apache-2.0"]
    assert deno_notices.spdx_ids("MIT/Apache-2.0") == ["MIT", "Apache-2.0"]
    assert deno_notices.spdx_ids("Apache-2.0 WITH LLVM-exception") == ["Apache-2.0", "LLVM-exception"]
    assert deno_notices.spdx_ids("(MIT OR Apache-2.0) AND Unicode-3.0") == ["MIT", "Apache-2.0", "Unicode-3.0"]
    assert deno_notices.spdx_ids("MPL-2.0+") == ["MPL-2.0"]


def test_license_files_in_package(tmp_path):
    for name in ("LICENSE-MIT", "LICENSE-APACHE", "COPYING", "README.md", "src.rs", "NOTICE"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    (tmp_path / "LICENSES").mkdir()
    (tmp_path / "LICENSES" / "Apache-2.0.txt").write_text("x", encoding="utf-8")
    names = [p.name for p in deno_notices.license_files(tmp_path)]
    assert names == ["COPYING", "LICENSE-APACHE", "LICENSE-MIT", "NOTICE", "Apache-2.0.txt"]


def test_rust_notices_match_pinned_deno():
    lock = json.loads((ROOT / "packaging" / "runtime" / "deno.lock").read_text(encoding="utf-8"))
    text = (LIC / "deno" / "THIRD_PARTY_RUST.txt").read_text(encoding="utf-8")
    first = text.splitlines()[0]
    assert first == f"Deno {lock['version']} (deno.exe, x86_64-pc-windows-msvc): Rust-компоненты и их лицензии"
    count = int(re.search(r"^Пакетов: (\d+)\.", text, re.M).group(1))
    listed = text.split("1. Перечень пакетов", 1)[1].split("2. Тексты лицензий", 1)[0]
    rows = [ln for ln in listed.splitlines() if re.match(r"^\S+ \d", ln)]
    assert len(rows) == count > 500
    assert re.search(r"^deno 2\.9\.7 ", listed, re.M) and re.search(r"^v8 150\.4\.0 ", listed, re.M)
    assert "(*)" not in listed


def test_v8_components_name_the_lgpl_part():
    text = (LIC / "deno" / "V8_COMPONENTS.txt").read_text(encoding="utf-8")
    assert f"крейт {deno_notices.V8_CRATE[0]} {deno_notices.V8_CRATE[1]}" in text
    assert "LGPL-2.1-or-later" in text and "GNU LESSER GENERAL PUBLIC LICENSE" in text.upper()
    assert "по самому бинарнику deno.exe состав" in text and "НЕ сверялся" in text
    for name, *_ in deno_notices.V8_COMPONENTS:
        assert f"* {name}" in text


def test_notices_reference_existing_files():
    notices = (LIC / "THIRD_PARTY_NOTICES.txt").read_text(encoding="utf-8")
    for ref in re.findall(r"licenses\\([\w\-\\.]+\.(?:txt|md)|[\w\-]+\\LICENSE)\b", notices):
        if "<" in ref or ref.startswith("python\\"):        # licenses\python\ формируется при сборке
            continue
        assert (LIC / ref.replace("\\", "/")).is_file(), ref
    sdk = (LIC / "webview2-sdk" / "VERSION").read_text(encoding="utf-8").strip()
    assert f"Microsoft WebView2 SDK {sdk}" in notices
