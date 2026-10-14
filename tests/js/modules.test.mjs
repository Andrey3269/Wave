// Граф ES-модулей фронтенда (S9): то, что браузер не проверит до запуска, проверяем заранее и без браузера.
//   node --test tests/js
// Входная точка берётся из frontend/index.html (<script type="module" src=...>); пока её нет, проверяется,
// что модулей нет вовсе. Ошибки здесь — это «белый экран» в WebView2: несуществующий файл, цикл импортов,
// загрузка из сети, модуль, который никто не подключил.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, existsSync, statSync } from "node:fs";
import { dirname, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
const FRONT = join(ROOT, "frontend");
const rel = (p) => relative(FRONT, p).split(sep).join("/");

function listJs(dir) {
  if (!existsSync(dir)) return [];
  return readdirSync(dir).flatMap((n) => {
    const p = join(dir, n);
    return statSync(p).isDirectory() ? listJs(p) : (n.endsWith(".js") ? [p] : []);
  });
}

/** Статические импорты и реэкспорты файла (комментарии и строки внутри кода импортами не считаются). */
function importsOf(src) {
  const code = src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/(^|[^:"'`\\])\/\/.*$/gm, "$1");
  const out = [];
  const re = /^\s*(?:import|export)\s+(?:[^"';]*?\s+from\s+)?["']([^"']+)["']/gm;
  for (let m; (m = re.exec(code));) out.push(m[1]);
  return out;
}

/** Имена, которые файл экспортирует (export const/let/function/class и export { a, b as c }). */
function exportsOf(src) {
  const names = new Set();
  for (const m of src.matchAll(/^\s*export\s+(?:async\s+)?(?:const|let|var|function\*?|class)\s+([A-Za-z_$][\w$]*)/gm)) names.add(m[1]);
  for (const m of src.matchAll(/^\s*export\s*\{([^}]*)\}/gm)) {
    for (const part of m[1].split(",").map((x) => x.trim()).filter(Boolean)) names.add(part.split(/\s+as\s+/).pop());
  }
  return names;
}

/** Именованные импорты файла: [{ spec, names }]. */
function namedImportsOf(src) {
  const out = [];
  for (const m of src.matchAll(/^\s*import\s*\{([^}]*)\}\s*from\s*["']([^"']+)["']/gm)) {
    out.push({ spec: m[2], names: m[1].split(",").map((x) => x.trim()).filter(Boolean).map((x) => x.split(/\s+as\s+/)[0]) });
  }
  return out;
}

function moduleEntry() {
  const html = readFileSync(join(FRONT, "index.html"), "utf8");
  const tags = [...html.matchAll(/<script\b([^>]*)>/g)].map((m) => m[1]);
  const mods = tags.filter((a) => /type=["']module["']/.test(a));
  assert.ok(mods.length <= 1, "входной модуль должен быть один");
  if (!mods.length) return null;
  const src = mods[0].match(/src=["']([^"']+)["']/);
  assert.ok(src, "у входного модуля нет src");
  assert.ok(!/^[a-z]+:|^\/\//i.test(src[1]), "входной модуль грузится с другого адреса");
  return resolve(FRONT, src[1]);
}

function graph(entry) {
  const nodes = new Map();
  const stack = [entry];
  while (stack.length) {
    const file = stack.pop();
    if (nodes.has(file)) continue;
    assert.ok(existsSync(file), `модуль не найден: ${rel(file)}`);
    const deps = importsOf(readFileSync(file, "utf8")).map((spec) => {
      assert.ok(spec.startsWith("./") || spec.startsWith("../"),
        `${rel(file)}: импорт «${spec}» — только относительные пути внутри frontend (без CDN и пакетов)`);
      assert.ok(spec.endsWith(".js"), `${rel(file)}: импорт «${spec}» без расширения .js не загрузится в браузере`);
      const target = resolve(dirname(file), spec);
      assert.ok(target.startsWith(FRONT + sep), `${rel(file)}: импорт «${spec}» выходит за пределы frontend`);
      return target;
    });
    nodes.set(file, deps);
    stack.push(...deps);
  }
  return nodes;
}

test("входной модуль и все импорты существуют, только относительные пути", () => {
  const entry = moduleEntry();
  if (!entry) {
    assert.deepEqual(listJs(join(FRONT, "js")).map(rel), [], "есть frontend/js, но index.html не подключает модуль");
    return;
  }
  graph(entry);
});

test("каждое импортируемое имя экспортируется целевым модулем", () => {
  const entry = moduleEntry();
  if (!entry) return;
  for (const file of graph(entry).keys()) {
    for (const { spec, names } of namedImportsOf(readFileSync(file, "utf8"))) {
      const target = resolve(dirname(file), spec);
      const exported = exportsOf(readFileSync(target, "utf8"));
      for (const n of names) assert.ok(exported.has(n), `${rel(file)}: «${n}» не экспортируется из ${rel(target)}`);
    }
  }
});

test("в графе модулей нет циклов", () => {
  const entry = moduleEntry();
  if (!entry) return;
  const nodes = graph(entry);
  const state = new Map();                 // 1 — в обходе, 2 — пройден
  const path = [];
  const visit = (f) => {
    if (state.get(f) === 2) return;
    if (state.get(f) === 1) assert.fail(`цикл импортов: ${[...path.slice(path.indexOf(f)), f].map(rel).join(" -> ")}`);
    state.set(f, 1); path.push(f);
    for (const d of nodes.get(f)) visit(d);
    path.pop(); state.set(f, 2);
  };
  visit(entry);
});

test("каждый файл frontend/js подключён (нет забытых модулей)", () => {
  const entry = moduleEntry();
  if (!entry) return;
  const reached = new Set(graph(entry).keys());
  const orphans = listJs(join(FRONT, "js")).filter((f) => !reached.has(f)).map(rel);
  assert.deepEqual(orphans, []);
});

test("нет динамического import() и загрузки скриптов из сети", () => {
  const files = [...listJs(join(FRONT, "js")), ...[join(FRONT, "app.js")].filter(existsSync)];
  for (const f of files) {
    const src = readFileSync(f, "utf8");
    assert.ok(!/\bimport\s*\(/.test(src), `${rel(f)}: динамический import()`);
    assert.ok(!/https?:\/\/[^"'\s]*(cdn|unpkg|jsdelivr|cdnjs|googleapis|cloudflare)/i.test(src), `${rel(f)}: адрес CDN`);
  }
});
