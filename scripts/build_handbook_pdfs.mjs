#!/usr/bin/env node
/**
 * One PDF PER handbook document, into docs_pdf/ — the sibling of
 * build_handbook_pdf.mjs, which binds the whole handbook into a single volume.
 *
 * Same pipeline, same rules: marked renders the markdown, Mermaid renders the
 * diagrams inside a real Chromium, and a diagram that will not parse FAILS the
 * build by name — a PDF with a red "diagram failed" box is not a deliverable.
 *
 *   BUILD_DEPS=~/prism-docs-build node scripts/build_handbook_pdfs.mjs [out-dir]
 *
 * BUILD_DEPS points at any directory whose node_modules holds
 * marked + mermaid + playwright-core (ESM ignores NODE_PATH, so they are
 * resolved by hand). The output directory defaults to docs_pdf/ at the repo
 * root; it is created if absent and existing PDFs are overwritten.
 */
import { createRequire } from 'module';
import { readFileSync, writeFileSync, readdirSync, existsSync, mkdirSync } from 'fs';
import { join, dirname, basename } from 'path';
import { fileURLToPath, pathToFileURL } from 'url';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = dirname(HERE);
const DOCS = join(REPO, 'docs', 'handbook');
const OUT_DIR = process.argv[2] || join(REPO, 'docs_pdf');

const DEPS = process.env.BUILD_DEPS || REPO;
const req = createRequire(pathToFileURL(join(DEPS, 'package.json')).href);
let chromium, marked;
try {
  ({ chromium } = req('playwright-core'));                                    // CJS
  ({ marked } = await import(pathToFileURL(req.resolve('marked')).href));     // ESM
} catch {
  console.error(`build dependencies missing under ${DEPS}\n`
    + `  mkdir -p ~/prism-docs-build && cd ~/prism-docs-build\n`
    + `  npm init -y && npm i marked mermaid playwright-core\n`
    + `  BUILD_DEPS=~/prism-docs-build node ${process.argv[1]}`);
  process.exit(1);
}

const REPO_URL = 'https://github.com/shashikanth-vh/vox/blob/claude/register-service-postgres-4tc9rj';
const CHROME = ['/opt/pw-browsers/chromium-1194/chrome-linux/chrome',
                '/opt/pw-browsers/chromium/chrome-linux/chrome']
  .find((p) => existsSync(p));
const MERMAID_JS = [process.env.MERMAID_JS || '',
                    join(DEPS, 'node_modules/mermaid/dist/mermaid.min.js')]
  .find((p) => p && existsSync(p));
if (!CHROME) { console.error('no Chromium found under /opt/pw-browsers'); process.exit(1); }
if (!MERMAID_JS) { console.error('mermaid.min.js not found — set BUILD_DEPS or MERMAID_JS'); process.exit(1); }

mkdirSync(OUT_DIR, { recursive: true });
marked.setOptions({ gfm: true, breaks: false, mangle: false, headerIds: false });

const files = readdirSync(DOCS).filter((f) => f.endsWith('.md')).sort();
const titleOf = (md, f) => (md.match(/^#\s+(.+)$/m)?.[1] || basename(f, '.md')).trim();

// Identical typography to the bound volume, minus its cover and contents pages —
// each document opens on its own title, ready to be filed or attached on its own.
const CSS = `
:root { --ink:#1a1d21; --dim:#5b6570; --rule:#dfe3e8; --accent:#0b5cad; --code-bg:#f5f7f9; }
* { box-sizing: border-box; }
body { font: 10.5pt/1.55 -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
       color: var(--ink); margin: 0; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
a { color: var(--accent); text-decoration: none; }
code, pre, kbd { font-family: "SF Mono", ui-monospace, "DejaVu Sans Mono", Menlo, Consolas, monospace; }
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
.mermaid-failed { border: 1px dashed #c0392b; color: #c0392b; padding: 8px; font-size: 9pt; }`;

const browser = await chromium.launch({ executablePath: CHROME, args: ['--no-sandbox'] });
const page = await browser.newPage();
const allFailures = [];

for (const f of files) {
  const raw = readFileSync(join(DOCS, f), 'utf8');
  const title = titleOf(raw, f);

  // Hold the mermaid fences aside so markdown cannot mangle them.
  const diagrams = [];
  const md = raw.replace(/```mermaid\n([\s\S]*?)```/g, (_, code) => {
    const id = `mmd-${diagrams.length}`;
    diagrams.push({ id, code: code.trim() });
    return `\n<!--MERMAID:${id}-->\n`;
  });

  let html = marked.parse(md);
  html = html.replace(/<!--MERMAID:(mmd-\d+)-->/g,
    (_, id) => `<div class="mermaid" id="${id}"></div>`);
  // Standalone PDFs have no in-document anchors to sibling files — every .md link
  // points at the repository instead.
  html = html.replace(/href="([^"#]+\.md)(#[^"]*)?"/g, (m, target) => {
    const clean = target.replace(/^(\.\.\/)+/, '').replace(/^\.\//, '');
    const prefix = target.startsWith('../../') ? '' : target.startsWith('../') ? 'docs/' : 'docs/handbook/';
    return `href="${REPO_URL}/${prefix}${clean}"`;
  });

  const doc = `<!doctype html><html><head><meta charset="utf-8"><title>${title}</title>
<style>${CSS}</style></head><body><div class="docfile">docs/handbook/${f}</div>${html}</body></html>`;
  const tmp = join(process.env.TMPDIR || '/tmp', `prism-doc-${basename(f, '.md')}.html`);
  writeFileSync(tmp, doc);

  await page.goto(pathToFileURL(tmp).href, { waitUntil: 'load' });
  if (diagrams.length) {
    await page.addScriptTag({ path: MERMAID_JS });
    const bad = await page.evaluate(async (defs) => {
      // eslint-disable-next-line no-undef
      const m = window.mermaid;
      m.initialize({
        startOnLoad: false, theme: 'neutral', securityLevel: 'loose',
        fontFamily: 'Segoe UI, Roboto, Helvetica Neue, Arial, sans-serif', fontSize: 13,
        flowchart: { useMaxWidth: true, htmlLabels: true, curve: 'basis',
                     nodeSpacing: 70, rankSpacing: 95, padding: 12 },
        sequence: { useMaxWidth: true, wrap: true },
        er: { useMaxWidth: true }, state: { useMaxWidth: true },
      });
      const out = [];
      for (const d of defs) {
        const host = document.getElementById(d.id);
        if (!host) continue;
        try {
          const { svg } = await m.render(d.id + '-svg', d.code);
          host.innerHTML = svg;
        } catch (e) {
          host.className = 'mermaid mermaid-failed';
          host.textContent = `diagram failed to render: ${String(e).slice(0, 200)}`;
          out.push({ id: d.id, error: String(e).slice(0, 300) });
        }
      }
      return out;
    }, diagrams);
    for (const b of bad) allFailures.push({ file: f, ...b });
  }

  const out = join(OUT_DIR, basename(f, '.md') + '.pdf');
  await page.emulateMedia({ media: 'print' });
  await page.pdf({
    path: out, format: 'A4', printBackground: true, outline: true, tagged: true,
    margin: { top: '16mm', bottom: '16mm', left: '15mm', right: '15mm' },
    displayHeaderFooter: true,
    headerTemplate: `<div style="font-size:7pt;color:#8a949e;width:100%;padding:0 15mm;">${title.replace(/[<>&]/g, '')}</div>`,
    footerTemplate: '<div style="font-size:7pt;color:#8a949e;width:100%;padding:0 15mm;text-align:right;">'
                  + '<span class="pageNumber"></span> / <span class="totalPages"></span></div>',
  });
  console.log(`  ${f.padEnd(34)} ${diagrams.length} diagram(s) -> ${basename(out)}`);
}

await browser.close();
if (allFailures.length) {
  console.error(`\n${allFailures.length} diagram(s) FAILED to render:`);
  for (const x of allFailures) console.error(`  ${x.file} ${x.id}: ${x.error}`);
}
console.log(`\nwrote ${files.length} PDFs to ${OUT_DIR}`);
process.exit(allFailures.length ? 1 : 0);
