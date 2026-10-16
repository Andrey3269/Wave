"""
Контраст цветовых токенов интерфейса по формуле WCAG 2.x (S12). Без браузера и без новых зависимостей: токены
читаются прямо из frontend/style.css, поэтому смена значения, нарушающая порог, сразу роняет тест.

Пары ниже — реально используемые сочетания «цвет текста или элемента / фон» (где именно — в столбце where) для тёмной
и светлой темы и для областей, которые всегда тёмные (.hero — баннер главной, .fs — полноэкранный плеер). Пороги:
  text  — обычный текст, 4.5:1 (WCAG 1.4.3 AA);
  large — крупный текст (от 24px или от 18.66px жирным), 3:1;
  ui    — значки, рамки полей, индикаторы состояния (WCAG 1.4.11), 3:1;
  focus — рамка фокуса относительно соседнего фона, 3:1.
Фон-картинка (обложка в баннере, на странице трека и в полноэкранном плеере) токенами не описывается: худший случай
посчитан в docs/engineering/reports/S12.md отдельно, здесь он не проверяется.

    python -m pytest tests/unit/test_contrast.py -q
    python tests/unit/test_contrast.py        # таблица значений (для отчёта)
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = Path(__file__).resolve().parents[2] / "frontend" / "style.css"
THRESHOLD = {"text": 4.5, "large": 3.0, "ui": 3.0, "focus": 3.0}


# ---------------------------------------------------------------- токены из style.css
def _block(css: str, selector: str) -> str:
    """Тело первого правила с точно таким селектором (без вложенных блоков: в style.css их нет)."""
    m = re.search(r"(?m)^\s*" + re.escape(selector) + r"\s*\{", css)
    assert m, f"в style.css нет правила {selector}"
    end = css.index("}", m.end())
    return css[m.end():end]


def _props(body: str) -> dict[str, str]:
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    return {k: v.strip() for k, v in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", body)}


def theme_tokens() -> dict[str, dict[str, str]]:
    css = CSS.read_text(encoding="utf-8")
    dark = _props(_block(css, ":root"))
    light = {**dark, **_props(_block(css, ':root[data-theme="light"]'))}
    hero, fs = _props(_block(css, ".hero")), _props(_block(css, ".fs"))
    return {
        "dark": dark, "light": light,
        # .hero и .fs переопределяют часть токенов поверх темы приложения
        "dark·hero": {**dark, **hero}, "light·hero": {**light, **hero},
        "dark·fs": {**dark, **fs}, "light·fs": {**light, **fs},
    }


def resolve(tokens: dict[str, str], value: str, depth: int = 0) -> str:
    assert depth < 10, f"цикл var(): {value}"
    m = re.fullmatch(r"var\((--[\w-]+)\)", value.strip())
    return resolve(tokens, tokens[m.group(1)], depth + 1) if m else value.strip()


# ---------------------------------------------------------------- цвет и контраст
def rgba(value: str) -> tuple[float, float, float, float]:
    v = value.strip()
    if v.startswith("#"):
        h = v[1:]
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0
    m = re.fullmatch(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)", v)
    assert m, f"не цвет: {value}"
    return float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4) or 1)


def over(fg: tuple, bg: tuple) -> tuple:
    """Полупрозрачный цвет поверх непрозрачного фона."""
    a = fg[3]
    return tuple(a * f + (1 - a) * b for f, b in zip(fg[:3], bg[:3])) + (1.0,)


def luminance(c: tuple) -> float:
    def ch(x: float) -> float:
        x /= 255
        return x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c[0]) + 0.7152 * ch(c[1]) + 0.0722 * ch(c[2])


def ratio(a: tuple, b: tuple) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def color(tokens: dict[str, str], spec: str, page: tuple | None = None) -> tuple:
    """spec: «--токен», «#hex» или «--токен over --фон» (полупрозрачный токен поверх фона)."""
    if " over " in spec:
        top, base = spec.split(" over ")
        return over(rgba(resolve(tokens, f"var({top})")), color(tokens, base))
    c = rgba(resolve(tokens, spec if spec.startswith("#") else f"var({spec})"))
    if c[3] < 1:
        assert page is not None, f"{spec} полупрозрачный — укажите фон через «over»"
        c = over(c, page)
    return c


# ---------------------------------------------------------------- реально используемые пары
# (области, цвет, фон, порог, где используется)
APP = ("dark", "light")
HERO = ("dark·hero", "light·hero")
FS = ("dark·fs", "light·fs")
PAIRS: list[tuple[tuple, str, str, str, str]] = [
    # основной текст
    (APP, "--text", "--bg", "text", "заголовки, названия треков, пункты меню на странице"),
    (APP, "--text", "--surface", "text", "мини-плеер, очередь, диалоги, карточки при наведении, строка при наведении"),
    (APP, "--text", "--surface-2", "text", "меню трека, кнопки-призраки, поля ввода, сообщение «нет сети»"),
    (APP, "--text", "--surface-3", "text", "уведомление (тост), пункт меню и кнопки при наведении"),
    # вторичный текст
    (APP, "--muted", "--bg", "text", "исполнитель и альбом в строке, статус, пустые состояния, подсказки, «Загрузить ещё»"),
    (APP, "--muted", "--surface", "text", "исполнитель в мини-плеере и очереди, время, подписи карточек, плейсхолдер поиска"),
    (APP, "--muted", "--surface-2", "text", "невыбранная вкладка поиска, подписи карточек при наведении, недоступная загрузка"),
    (APP, "--muted", "--accent-soft over --bg", "text", "подсказки на плитках коллекции"),
    # акцент как цвет текста и значков
    (APP, "--accent-ink", "--bg", "text", "играющий трек, тип страницы, надзаголовок, сообщение настроек, % загрузки"),
    (APP, "--accent-ink", "--surface", "text", "играющий трек при наведении и в очереди, нажатые кнопки мини-плеера"),
    (APP, "--accent-ink", "--surface-2", "text", "нажатые кнопки-пилюли («Нравится», «Текст»), «Скачано»"),
    (APP, "--accent-ink", "--surface-3", "text", "«Удалить плейлист» при наведении"),
    (APP, "--accent-ink", "--accent-soft over --bg", "text", "активный пункт бокового меню, значок плитки коллекции"),
    (APP, "--accent-ink", "--accent-soft over --surface", "text", "активный пункт бокового меню при наведении"),
    # текст на акцентной заливке
    (APP, "--on-accent", "--accent", "text", "«Слушать», выбранный источник, буква аватара, Play"),
    (APP, "--on-accent", "--accent-hover", "text", "те же кнопки при наведении"),
    # выбранная вкладка (инверсия)
    (APP, "--bg", "--text", "text", "выбранная вкладка поиска"),
    # неактивные элементы и значки
    (APP, "--faint", "--bg", "ui", "значок «скачивание недоступно», стрелки «›» (поля ввода — рамка)"),
    (APP, "--faint", "--surface", "ui", "те же значки на строке при наведении, рамка поля в диалоге"),
    (APP, "--faint", "--surface-2", "ui", "стрелка «›» плитки коллекции при наведении"),
    (APP, "--accent", "--bg", "ui", "рамка нажатой кнопки, заливка шкалы, рамка поиска в фокусе"),
    (APP, "--accent", "--surface", "ui", "рамка поля поиска в фокусе (поле — --surface)"),
    (APP, "--accent", "--surface-2", "ui", "рамка поля ввода в фокусе, рамка нажатой пилюли"),
    (APP, "--text", "--bar", "ui", "заполненная часть шкалы прогресса и громкости относительно пустой"),
    # рамка фокуса (outline: var(--accent-ink))
    (APP, "--accent-ink", "--bg", "focus", "рамка фокуса на странице"),
    (APP, "--accent-ink", "--surface", "focus", "рамка фокуса в мини-плеере, очереди, диалогах"),
    (APP, "--accent-ink", "--surface-2", "focus", "рамка фокуса пункта меню"),
    (APP, "--accent-ink", "--surface-3", "focus", "рамка фокуса пункта меню при наведении"),
    # тексты песен (22px жирным — крупный текст; без синхронизации — 16px)
    (APP, "--ly", "--surface", "large", "следующие строки текста песни на странице трека"),
    (APP, "--ly-past", "--surface", "large", "прошедшие строки текста песни"),
    (APP, "--ly-plain", "--surface", "text", "текст песни без таймкодов"),
    # баннер главной (всегда тёмный фон #141416 под текстом)
    (HERO, "--text", "#141416", "large", "название трека в баннере"),
    (HERO, "--muted", "#141416", "text", "длительность, источник, прослушивания"),
    (HERO, "--accent-ink", "#141416", "text", "надзаголовок «Сейчас играет» / название подборки"),
    (HERO, "--on-accent", "--accent", "text", "кнопка «Слушать подборку» / «Пауза»"),
    (HERO, "--text", "--surface-2", "ui", "круглые кнопки баннера"),
    (HERO, "--accent-ink", "#141416", "focus", "рамка фокуса в баннере"),
    # полноэкранный плеер: панель очереди — сплошной --surface
    (FS, "--text", "--surface", "text", "очередь: названия"),
    (FS, "--muted", "--surface", "text", "очередь: исполнители, длительность, заголовки «Далее»"),
    (FS, "--muted", "--surface-2", "text", "очередь: играющая строка и строка при наведении"),
    (FS, "--faint", "--surface", "text", "очередь: пояснения («Уже сыграно», перемешивание)"),
    (FS, "--accent-ink", "--surface-2", "text", "очередь: название играющего трека"),
    (FS, "--on-accent", "--accent", "text", "кнопка Play/Pause"),
    (FS, "--accent-ink", "--surface", "focus", "рамка фокуса в панели очереди"),
]


def cases():
    for scopes, fg, bg, kind, where in PAIRS:
        for scope in scopes:
            yield pytest.param(scope, fg, bg, kind, where, id=f"{scope}:{fg}/{bg}")


def measure(tokens: dict[str, str], fg: str, bg: str) -> float:
    back = color(tokens, bg, page=color(tokens, "--bg"))
    return ratio(color(tokens, fg, page=back), back)


@pytest.mark.parametrize("scope,fg,bg,kind,where", list(cases()))
def test_contrast_pair(scope, fg, bg, kind, where):
    r = measure(theme_tokens()[scope], fg, bg)
    assert r >= THRESHOLD[kind], f"{scope}: {fg} на {bg} = {r:.2f}:1 < {THRESHOLD[kind]}:1 ({kind}; {where})"


def test_accent_as_foreground_uses_the_text_token():
    """Акцент как цвет текста и значков — только через --accent-ink (в светлой теме он темнее заливки --accent)."""
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    bad = re.findall(r"(?<![\w-])color\s*:\s*var\(--accent\)", css)
    assert not bad, f"color: var(--accent) встречается {len(bad)} раз — используйте var(--accent-ink)"


def test_focus_outline_uses_the_text_accent():
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    outlines = re.findall(r"outline\s*:\s*2px solid var\((--[\w-]+)\)", css)
    assert outlines and set(outlines) == {"--accent-ink"}, outlines


def test_faint_is_not_a_text_colour_on_app_surfaces():
    """--faint в приложении — для значков и рамок (3:1); текст «Скачивание недоступно» — вторичным цветом."""
    css = re.sub(r"/\*.*?\*/", "", CSS.read_text(encoding="utf-8"), flags=re.S)
    rule = re.search(r"\.abtn\.is-denied\s*\{([^}]*)\}", css)
    assert rule and "var(--muted)" in rule.group(1)


def table() -> str:
    """Таблица для отчёта: область, токены, коэффициент, порог, результат."""
    rows = ["| Тема / область | Цвет / фон | Контраст | Критерий | Результат | Где |", "|---|---|---|---|---|---|"]
    tok = theme_tokens()
    for scopes, fg, bg, kind, where in PAIRS:
        for scope in scopes:
            r = measure(tok[scope], fg, bg)
            ok = "PASS" if r >= THRESHOLD[kind] else "FAIL"
            rows.append(f"| {scope} | `{fg}` / `{bg}` | {r:.2f}:1 | {kind} ≥ {THRESHOLD[kind]}:1 | {ok} | {where} |")
    return "\n".join(rows)


if __name__ == "__main__":
    print(table())
