#!/usr/bin/env node
/**
 * Build the PRISM Handbook as ONE self-contained PDF, diagrams and all.
 *
 * Why a PDF: the handbook lives in git, where Mermaid renders and diffs. Confluence
 * renders neither Markdown files nor Mermaid, so a mirror there has to be something a
 * browser can already draw. A PDF attachment previews inline in Confluence, its text is
 * indexed by Confluence search, and it carries the diagrams as vectors — one artefact to
 * re-attach when the docs change, instead of sixteen pages to re-paste.
 *
 * The diagrams are rendered by real Mermaid in real Chromium, so this doubles as a
 * validator: a diagram that will not parse fails the build by name rather than shipping
 * as a blank box.
 *
 *   node scripts/build_handbook_pdf.mjs [outfile.pdf]
 *
 * Needs `marked`, `mermaid` and `playwright-core` on NODE_PATH (see BUILD_DEPS below).
 */

import { createRequire } from 'module';
import { readFileSync, writeFileSync, existsSync, readdirSync } from 'fs';
import { join, dirname, basename } from 'path';
import { fileURLToPath, pathToFileURL } from 'url';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = dirname(HERE);
const DOCS = join(REPO, 'docs', 'handbook');
const OUT = process.argv[2] || join(REPO, 'PRISM-Handbook.pdf');

// The build dependencies are NOT vendored into the repo — this is a docs tool, not a
// runtime component. Point BUILD_DEPS at any directory holding a node_modules with
// marked + mermaid + playwright-core; ESM ignores NODE_PATH, so resolve them by hand.
const DEPS = process.env.BUILD_DEPS || REPO;
const req = createRequire(pathToFileURL(join(DEPS, 'package.json')).href);
let chromium, marked;
try {
  // playwright-core is CommonJS (importing it yields a default wrapper); marked is ESM.
  ({ chromium } = req('playwright-core'));
  ({ marked } = await import(pathToFileURL(req.resolve('marked')).href));
} catch {
  console.error(`build dependencies missing under ${DEPS}\n`
    + `  mkdir -p ~/prism-docs-build && cd ~/prism-docs-build\n`
    + `  npm init -y && npm i marked mermaid playwright-core\n`
    + `  BUILD_DEPS=~/prism-docs-build node ${process.argv[1]}`);
  process.exit(1);
}

// Relative links that leave the handbook cannot resolve inside a PDF — point them at the
// repository instead, so a reader who wants the source file can still reach it.
const REPO_URL = 'https://github.com/shashikanth-vh/vox/blob/claude/register-service-postgres-4tc9rj';

// A Chromium that is already on the box; downloading one is neither needed nor allowed.
const CHROME = ['/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
                '/opt/pw-browsers/chromium/chrome-linux/chrome']
  .find((p) => existsSync(p));

const MERMAID_JS = [process.env.MERMAID_JS || '',
                    join(DEPS, 'node_modules/mermaid/dist/mermaid.min.js')]
  .find((p) => p && existsSync(p));

if (!CHROME) { console.error('no Chromium found under /opt/pw-browsers'); process.exit(1); }
if (!MERMAID_JS) { console.error('mermaid.min.js not found — set BUILD_DEPS or MERMAID_JS'); process.exit(1); }

// ── the documents, in reading order ──────────────────────────────────────────
const files = readdirSync(DOCS).filter((f) => f.endsWith('.md')).sort();
const ordered = ['README.md', ...files.filter((f) => f !== 'README.md')];

const slug = (f) => 'doc-' + basename(f, '.md').toLowerCase();
const titleOf = (md, f) => (md.match(/^#\s+(.+)$/m)?.[1] || basename(f, '.md')).trim();

// ── markdown → html, one section per document ────────────────────────────────
marked.setOptions({ gfm: true, breaks: false, mangle: false, headerIds: false });

const diagrams = [];          // {id, code, file} — rendered in the browser below
const sections = [];

for (const f of ordered) {
  const raw = readFileSync(join(DOCS, f), 'utf8');
  const title = titleOf(raw, f);

  // Pull the mermaid fences out BEFORE markdown runs, so nothing mangles the source.
  const held = [];
  const md = raw.replace(/```mermaid\n([\s\S]*?)```/g, (_, code) => {
    const id = `mmd-${diagrams.length}`;
    diagrams.push({ id, code: code.trim(), file: f });
    held.push(id);
    return `\n<!--MERMAID:${id}-->\n`;
  });

  let html = marked.parse(md);

  // Placeholders → the divs Mermaid will replace.
  html = html.replace(/<!--MERMAID:(mmd-\d+)-->/g,
    (_, id) => `<div class="mermaid" id="${id}"></div>`);

  // Links: within the handbook → an in-PDF anchor; outside it → the repository.
  html = html.replace(/href="([^"#]+\.md)(#[^"]*)?"/g, (m, target) => {
    const name = basename(target);
    if (!target.includes('/') && ordered.includes(name)) return `href="#${slug(name)}"`;
    const clean = target.replace(/^(\.\.\/)+/, '').replace(/^\.\//, '');
    const prefix = target.startsWith('../../') ? '' : target.startsWith('../') ? 'docs/' : 'docs/handbook/';
    return `href="${REPO_URL}/${prefix}${clean}"`;
  });
  html = html.replace(/href="(\.\.\/\.\.\/[^"]+|\.\.\/[^"]+)"/g, (m, target) => {
    const clean = target.replace(/^(\.\.\/)+/, '');
    return `href="${REPO_URL}/${target.startsWith('../../') ? '' : 'docs/'}${clean}"`;
  });

  sections.push({ file: f, id: slug(f), title, html });
  console.log(`  ${f.padEnd(34)} ${held.length} diagram(s)`);
}

// ── the page ─────────────────────────────────────────────────────────────────
const toc = sections.map((s) =>
  `<li><a href="#${s.id}"><span class="t">${s.title}</span><span class="f">${s.file}</span></a></li>`).join('\n');

const body = sections.map((s, i) => `
<section class="doc${i === 0 ? ' first' : ''}" id="${s.id}">
  <div class="docfile">${s.file}</div>
  ${s.html}
</section>`).join('\n');

const page = `<!doctype html><html><head><meta charset="utf-8"><title>PRISM Handbook</title>
<style>
:root { --ink:#1a1d21; --dim:#5b6570; --rule:#dfe3e8; --accent:#0b5cad; --code-bg:#f5f7f9; }
* { box-sizing: border-box; }
body { font: 10.5pt/1.55 -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
       color: var(--ink); margin: 0; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
a { color: var(--accent); text-decoration: none; }
code, pre, kbd { font-family: "SF Mono", ui-monospace, "DejaVu Sans Mono", Menlo, Consolas, monospace; }

/* cover */
.cover { height: 247mm; display: flex; flex-direction: column; justify-content: center;
         page-break-after: always; text-align: center; }
.cover h1 { font-size: 34pt; margin: 0 0 6px; letter-spacing: -0.5px; }
.cover .sub { font-size: 13pt; color: var(--dim); margin-bottom: 30px; }
.cover .meta { font-size: 9pt; color: var(--dim); line-height: 1.9; }
.cover .rule { width: 70px; height: 3px; background: var(--accent); margin: 22px auto; }

/* contents */
.toc { page-break-after: always; }
.toc h2 { font-size: 17pt; border: 0; margin: 0 0 14px; }
.toc ol { list-style: none; padding: 0; counter-reset: c; }
.toc li { counter-increment: c; border-bottom: 1px solid var(--rule); }
.toc a { display: flex; align-items: baseline; gap: 10px; padding: 7px 2px; color: var(--ink); }
.toc a::before { content: counter(c, decimal-leading-zero); color: var(--accent);
                 font-variant-numeric: tabular-nums; font-weight: 600; font-size: 9pt; min-width: 22px; }
.toc .t { flex: 1; }
.toc .f { color: var(--dim); font-size: 8.5pt; font-family: "SF Mono", ui-monospace, monospace; }

/* documents */
.doc { page-break-before: always; }
.doc.first { page-break-before: avoid; }
.docfile { font-size: 8pt; color: var(--dim); font-family: "SF Mono", ui-monospace, monospace;
           border-bottom: 1px solid var(--rule); padding-bottom: 4px; margin-bottom: 14px; }
h1 { font-size: 20pt; margin: 0 0 10px; letter-spacing: -0.3px; page-break-after: avoid; }
h2 { font-size: 13.5pt; margin: 22px 0 8px; padding-top: 8px; border-top: 1px solid var(--rule);
     page-break-after: avoid; }
h3 { font-size: 11.5pt; margin: 16px 0 6px; page-break-after: avoid; }
h4 { font-size: 10.5pt; margin: 12px 0 4px; page-break-after: avoid; }
p, ul, ol { margin: 0 0 9px; }
li { margin-bottom: 3px; }
blockquote { margin: 10px 0; padding: 8px 14px; border-left: 3px solid var(--accent);
             background: #f4f8fc; color: #253040; page-break-inside: avoid; }
blockquote p:last-child { margin-bottom: 0; }
hr { border: 0; border-top: 1px solid var(--rule); margin: 18px 0; }

table { border-collapse: collapse; width: 100%; margin: 10px 0 14px; font-size: 9pt;
        page-break-inside: avoid; }
th, td { border: 1px solid var(--rule); padding: 5px 8px; text-align: left; vertical-align: top; }
th { background: #eef2f6; font-weight: 600; }
tr:nth-child(even) td { background: #fafbfc; }

pre { background: var(--code-bg); border: 1px solid var(--rule); border-radius: 4px;
      padding: 9px 11px; overflow: hidden; white-space: pre-wrap; word-break: break-word;
      font-size: 8.4pt; line-height: 1.45; page-break-inside: avoid; margin: 8px 0 12px; }
:not(pre) > code { background: var(--code-bg); border: 1px solid var(--rule); border-radius: 3px;
                   padding: 0.5px 4px; font-size: 8.8pt; word-break: break-word; }

.mermaid { margin: 12px 0 16px; text-align: center; page-break-inside: avoid; }
.mermaid svg { max-width: 100% !important; height: auto !important; }
.mermaid-failed { border: 1px dashed #c0392b; color: #c0392b; padding: 8px; font-size: 9pt; }
</style></head><body>

<div class="cover">
  <h1>The PRISM Handbook</h1>
  <div class="sub">Architecture, deployment, operation and use</div>
  <div class="rule"></div>
  <div class="meta">
    Evam Finance &middot; PRISM platform<br>
    ${sections.length} documents &middot; ${diagrams.length} diagrams<br>
    Generated from <code>docs/handbook/</code>
  </div>
</div>

<div class="toc"><h2>Contents</h2><ol>${toc}</ol></div>
${body}
</body></html>`;

const tmpHtml = join(process.env.TMPDIR || '/tmp', 'prism-handbook.html');
writeFileSync(tmpHtml, page);

// ── render ───────────────────────────────────────────────────────────────────
console.log(`\nrendering ${diagrams.length} diagrams in Chromium…`);
const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox'] });
const p = await browser.newPage();
await p.goto(pathToFileURL(tmpHtml).href, { waitUntil: 'load' });
await p.addScriptTag({ path: MERMAID_JS });

const failures = await p.evaluate(async (defs) => {
  // eslint-disable-next-line no-undef
  const m = window.mermaid;
  m.initialize({
    startOnLoad: false, theme: 'neutral', securityLevel: 'loose',
    fontFamily: 'Segoe UI, Roboto, Helvetica Neue, Arial, sans-serif', fontSize: 13,
    // Generous spacing: several maps carry long edges that skip ranks, and at the default
    // 50/50 their labels land on top of each other where the edges cross.
    flowchart: { useMaxWidth: true, htmlLabels: true, curve: 'basis',
                 nodeSpacing: 70, rankSpacing: 95, padding: 12 },
    sequence: { useMaxWidth: true, wrap: true },
    er: { useMaxWidth: true }, state: { useMaxWidth: true },
  });
  const bad = [];
  for (const d of defs) {
    const host = document.getElementById(d.id);
    if (!host) continue;
    try {
      const { svg } = await m.render(d.id + '-svg', d.code);
      host.innerHTML = svg;
    } catch (e) {
      host.className = 'mermaid mermaid-failed';
      host.textContent = `diagram failed to render: ${String(e).slice(0, 200)}`;
      bad.push({ id: d.id, file: d.file, error: String(e).slice(0, 300) });
    }
  }
  return bad;
}, diagrams);

if (failures.length) {
  console.error(`\n${failures.length} diagram(s) FAILED to render:`);
  for (const f of failures) console.error(`  ${f.file} ${f.id}: ${f.error}`);
} else {
  console.log('all diagrams rendered');
}

// A quick eyeball of the styling without opening a PDF viewer: HANDBOOK_PREVIEW=/tmp/pre
// writes <prefix>-cover.png and <prefix>-<section>.png straight from the rendered page.
if (process.env.HANDBOOK_PREVIEW) {
  const pre = process.env.HANDBOOK_PREVIEW;
  await p.setViewportSize({ width: 900, height: 1250 });
  await p.locator('.cover').screenshot({ path: `${pre}-cover.png` });
  for (const id of (process.env.HANDBOOK_PREVIEW_IDS || 'doc-01-architecture').split(',')) {
    const el = p.locator(`#${id.trim()}`);
    if (await el.count()) await el.screenshot({ path: `${pre}-${id.trim()}.png` });
  }
  console.log(`previews written to ${pre}-*.png`);
}

await p.emulateMedia({ media: 'print' });
await p.pdf({
  path: OUT, format: 'A4', printBackground: true, outline: true, tagged: true,
  margin: { top: '16mm', bottom: '16mm', left: '15mm', right: '15mm' },
  displayHeaderFooter: true,
  headerTemplate: '<div style="font-size:7pt;color:#8a949e;width:100%;padding:0 15mm;">The PRISM Handbook</div>',
  footerTemplate: '<div style="font-size:7pt;color:#8a949e;width:100%;padding:0 15mm;text-align:right;">'
                + '<span class="pageNumber"></span> / <span class="totalPages"></span></div>',
});

await browser.close();
console.log(`\nwrote ${OUT}`);
process.exit(failures.length ? 1 : 0);
