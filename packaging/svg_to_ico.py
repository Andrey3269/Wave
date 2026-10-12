"""
Делает иконку приложения (.ico) из SVG-логотипа без внешних конвертеров: нужен только Pillow.

    python packaging/svg_to_ico.py frontend/assets/wave_icon.svg packaging/wave.ico

Поддержаны простые SVG: rect (со скруглением), path (M L H V C S Q A Z, абсолютные и относительные),
заливка цветом или linearGradient. Этого достаточно для логотипа Wave.
"""
from __future__ import annotations

import math
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

SS = 1024                        # размер промежуточного рисунка (потом уменьшаем: получается гладкий край)
NAMED = {"snow": "#fffafa", "white": "#ffffff", "black": "#000000"}
NUM = r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"


def color(value: str) -> tuple[int, int, int]:
    value = NAMED.get(value.strip().lower(), value.strip())
    if value.startswith("#"):
        h = value[1:]
        h = "".join(c * 2 for c in h) if len(h) == 3 else h
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    raise ValueError(f"Неподдерживаемый цвет: {value}")


def _arc(x1, y1, rx, ry, phi, fa, fs, x2, y2, n=32):
    if rx == 0 or ry == 0:
        return [(x2, y2)]
    phi = math.radians(phi)
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p, y1p = cp * dx + sp * dy, -sp * dx + cp * dy
    rx, ry = abs(rx), abs(ry)
    lam = x1p**2 / rx**2 + y1p**2 / ry**2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    den = rx**2 * y1p**2 + ry**2 * x1p**2
    co = math.sqrt(max(0.0, (rx**2 * ry**2 - rx**2 * y1p**2 - ry**2 * x1p**2) / den)) * (-1 if fa == fs else 1)
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx, cy = cp * cxp - sp * cyp + (x1 + x2) / 2, sp * cxp + cp * cyp + (y1 + y2) / 2
    ang = lambda ux, uy, vx, vy: math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
    th1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dth = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not fs and dth > 0:
        dth -= 2 * math.pi
    if fs and dth < 0:
        dth += 2 * math.pi
    pts = []
    for k in range(1, n + 1):
        t = th1 + dth * k / n
        pts.append((cp * rx * math.cos(t) - sp * ry * math.sin(t) + cx, sp * rx * math.cos(t) + cp * ry * math.sin(t) + cy))
    return pts


def flatten(d: str) -> list[list[tuple[float, float]]]:
    """Контур SVG -> список подконтуров (ломаных)."""
    toks = re.findall(rf"[MmLlHhVvCcSsQqTtAaZz]|{NUM}", d)
    i, cmd, x, y, sx, sy = 0, None, 0.0, 0.0, 0.0, 0.0
    subs: list[list[tuple[float, float]]] = []
    cur: list[tuple[float, float]] = []
    last_c = None

    def num():
        nonlocal i
        i += 1
        return float(toks[i - 1])

    def bez(p0, p1, p2, p3, n=24):
        return [((1 - t) ** 3 * p0[0] + 3 * (1 - t) ** 2 * t * p1[0] + 3 * (1 - t) * t * t * p2[0] + t**3 * p3[0],
                 (1 - t) ** 3 * p0[1] + 3 * (1 - t) ** 2 * t * p1[1] + 3 * (1 - t) * t * t * p2[1] + t**3 * p3[1])
                for t in (k / n for k in range(1, n + 1))]

    while i < len(toks):
        if toks[i].isalpha():
            cmd = toks[i]
            i += 1
            if cmd in "Zz":
                if cur:
                    subs.append(cur)
                cur, x, y, last_c = [], sx, sy, None
            continue
        rel, C = cmd.islower(), cmd.upper()
        if not cur and C != "M":
            cur = [(x, y)]
        ox, oy = (x, y) if rel else (0.0, 0.0)
        if C == "M":
            nx, ny = num() + ox, num() + oy
            if cur:
                subs.append(cur)
            cur, x, y, sx, sy, last_c = [(nx, ny)], nx, ny, nx, ny, None
            cmd = "l" if rel else "L"
        elif C == "L":
            x, y = num() + ox, num() + oy
            cur.append((x, y)); last_c = None
        elif C == "H":
            x = num() + (x if rel else 0.0)
            cur.append((x, y)); last_c = None
        elif C == "V":
            y = num() + (y if rel else 0.0)
            cur.append((x, y)); last_c = None
        elif C == "C":
            p1 = (num() + ox, num() + oy); p2 = (num() + ox, num() + oy); p3 = (num() + ox, num() + oy)
            cur += bez((x, y), p1, p2, p3); x, y = p3; last_c = p2
        elif C == "S":
            p1 = (2 * x - last_c[0], 2 * y - last_c[1]) if last_c else (x, y)
            p2 = (num() + ox, num() + oy); p3 = (num() + ox, num() + oy)
            cur += bez((x, y), p1, p2, p3); x, y = p3; last_c = p2
        elif C == "Q":
            q = (num() + ox, num() + oy); p3 = (num() + ox, num() + oy)
            p1 = (x + 2 / 3 * (q[0] - x), y + 2 / 3 * (q[1] - y)); p2 = (p3[0] + 2 / 3 * (q[0] - p3[0]), p3[1] + 2 / 3 * (q[1] - p3[1]))
            cur += bez((x, y), p1, p2, p3); x, y = p3; last_c = None
        elif C == "A":
            rx, ry, rot, fa, fs = num(), num(), num(), int(num()), int(num())
            nx, ny = num() + ox, num() + oy
            cur += _arc(x, y, rx, ry, rot, fa, fs, nx, ny); x, y = nx, ny; last_c = None
        else:
            raise ValueError(f"Команда пути не поддерживается: {cmd}")
    if cur:
        subs.append(cur)
    return subs


def render(svg_path: Path) -> Image.Image:
    root = ET.parse(svg_path).getroot()
    strip = lambda t: t.split("}")[-1]
    vb = [float(v) for v in root.attrib["viewBox"].split()]
    scale = SS / max(vb[2], vb[3])
    out = Image.new("RGBA", (SS, SS), (0, 0, 0, 0))
    ox, oy = (SS - vb[2] * scale) / 2, (SS - vb[3] * scale) / 2        # по центру, если логотип не квадратный

    grads = {}
    for g in root.iter():
        if strip(g.tag) == "linearGradient":
            stops = [(float(s.attrib.get("offset", 0)), color(s.attrib.get("stop-color", "#000"))) for s in g if strip(s.tag) == "stop"]
            grads[g.attrib["id"]] = ({k: float(g.attrib[k]) for k in ("x1", "y1", "x2", "y2")}, sorted(stops))

    def paint_layer(mask: Image.Image, fill: str) -> None:
        if fill.startswith("url(#"):
            g, stops = grads[fill[5:-1]]
            x1, y1, x2, y2 = (g[k] for k in ("x1", "y1", "x2", "y2"))
            lay = Image.new("RGB", (SS, SS))
            px = lay.load()
            vx, vy = x2 - x1, y2 - y1
            ll = vx * vx + vy * vy
            for py in range(SS):
                uy = (py - oy) / scale
                for pxx in range(SS):
                    ux = (pxx - ox) / scale
                    t = min(1.0, max(0.0, ((ux - x1) * vx + (uy - y1) * vy) / ll))
                    for (o0, c0), (o1, c1) in zip(stops, stops[1:]):
                        if t <= o1 or (o1, c1) == stops[-1]:
                            f = 0 if o1 == o0 else min(1.0, max(0.0, (t - o0) / (o1 - o0)))
                            px[pxx, py] = tuple(round(a + (b - a) * f) for a, b in zip(c0, c1))
                            break
        else:
            lay = Image.new("RGB", (SS, SS), color(fill))
        out.paste(lay, (0, 0), mask.convert("L"))

    for el in root.iter():
        tag, fill = strip(el.tag), el.attrib.get("fill", "#000")
        if fill == "none":
            continue
        mask = Image.new("1", (SS, SS), 0)
        if tag == "rect":
            x, y, w, h = (float(el.attrib.get(k, 0)) for k in ("x", "y", "width", "height"))
            r = float(el.attrib.get("rx", 0))
            ImageDraw.Draw(mask).rounded_rectangle(
                (ox + x * scale, oy + y * scale, ox + (x + w) * scale - 1, oy + (y + h) * scale - 1), radius=r * scale, fill=1)
        elif tag == "path":
            for sub in flatten(el.attrib["d"]):               # чётно-нечётное правило: «дырки» вырезаются
                m = Image.new("1", (SS, SS), 0)
                ImageDraw.Draw(m).polygon([(ox + px_ * scale, oy + py_ * scale) for px_, py_ in sub], fill=1)
                mask = ImageChops.logical_xor(mask, m)
        else:
            continue
        paint_layer(mask, fill)
    return out


if __name__ == "__main__":
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    img = render(src)
    img.save(dst, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("Иконка:", dst)