import { Fragment, type ReactNode } from 'react';
import { Box, Typography } from '@mui/material';
import { tokens } from '../../theme';

/**
 * The CAM as the committee reads it — headings, prose, lists and full tables — instead of
 * the raw Markdown the workbench used to print. Signal cells are shaded as the Word CAM
 * shades them (GREEN / AMBER / RED, and a severity — High / Moderate / Low — in a column
 * headed Severity, Risk, Rating or Signal), so the credit picture reads at a glance here
 * too.
 *
 * Deliberately small and dependency-free: it builds React elements only (no HTML
 * strings, so nothing a model writes can inject markup), and understands exactly the
 * Markdown the CAM pipeline emits.
 */

const SIGNAL: Record<string, { bg: string; fg: string }> = {
  GREEN: { bg: '#C6EFCE', fg: '#006100' },
  AMBER: { bg: '#FFEB9C', fg: '#7F5200' },
  RED: { bg: '#FFC7CE', fg: '#9C0006' },
};
const SEVERITY: Record<string, string> = {
  high: 'RED', critical: 'RED', moderate: 'AMBER', medium: 'AMBER', low: 'GREEN',
};

export function signalOf(cell: string, header = ''): { bg: string; fg: string } | null {
  const plain = cell.replace(/[*_`]/g, '').trim();
  const m = /^(GREEN|AMBER|RED)\b/.exec(plain);
  if (m) return SIGNAL[m[1]];
  if (/severity|risk|rating|signal|grade/i.test(header) && plain.split(/\s+/).length <= 4) {
    const s = SEVERITY[(plain.split(/\s+/)[0] || '').toLowerCase()];
    if (s) return SIGNAL[s];
  }
  return null;
}

/** **bold**, *italic*, `code` → elements. */
function inline(text: string, key = ''): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|__[^_]+__|\*[^*\n]+\*|_[^_\n]+_|`[^`\n]+`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let n = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const t = m[0];
    const k = `${key}-${n++}`;
    if (t.startsWith('**') || t.startsWith('__')) out.push(<strong key={k}>{t.slice(2, -2)}</strong>);
    else if (t.startsWith('`')) out.push(<code key={k}>{t.slice(1, -1)}</code>);
    else out.push(<em key={k}>{t.slice(1, -1)}</em>);
    last = m.index + t.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const cells = (line: string) => line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|')
  .map((c) => c.trim());
const isRule = (line: string) => /^\s*\|?[\s:|-]+\|?\s*$/.test(line) && line.includes('-');

function Table({ rows, keyBase }: { rows: string[]; keyBase: string }) {
  const head = rows.length > 1 && isRule(rows[1]) ? cells(rows[0]) : null;
  const body = (head ? rows.slice(2) : rows).filter((r) => !isRule(r)).map(cells);
  return (
    <Box sx={{ overflowX: 'auto', my: 1 }}>
      <Box component="table" sx={{ borderCollapse: 'collapse', width: '100%', fontSize: 12 }}>
        {head && (
          <thead>
            <tr>{head.map((h, i) => (
              <Box component="th" key={i} sx={{ bgcolor: tokens.navy ?? '#1F3864', color: '#fff',
                textAlign: 'left', px: 0.8, py: 0.5, border: '1px solid #BFBFBF', fontWeight: 600 }}>
                {inline(h, `${keyBase}h${i}`)}
              </Box>))}
            </tr>
          </thead>
        )}
        <tbody>
          {body.map((r, ri) => (
            <tr key={ri}>
              {r.map((c, ci) => {
                const sig = signalOf(c, head?.[ci] ?? '');
                const numeric = ci > 0 && /^[-−(]?[\d,.]+[)%x]?$/.test(c.replace(/[*\s]/g, ''));
                return (
                  <Box component="td" key={ci} sx={{
                    px: 0.8, py: 0.4, border: '1px solid #BFBFBF', verticalAlign: 'top',
                    textAlign: numeric ? 'right' : 'left',
                    bgcolor: sig ? sig.bg : (ri % 2 ? '#F3F6FA' : 'transparent'),
                    color: sig ? sig.fg : 'inherit', fontWeight: sig ? 600 : 400,
                  }}>{inline(c, `${keyBase}${ri}-${ci}`)}</Box>
                );
              })}
            </tr>
          ))}
        </tbody>
      </Box>
    </Box>
  );
}

export default function CamMarkdown({ text }: { text: string }) {
  const lines = (text || '').split('\n');
  const out: ReactNode[] = [];
  let i = 0;
  let para: string[] = [];
  const flush = () => {
    if (para.length) {
      out.push(<Typography key={`p${i}-${out.length}`} sx={{ fontSize: 12.8, lineHeight: 1.6, my: 0.6 }}>
        {inline(para.join(' '), `p${out.length}`)}</Typography>);
      para = [];
    }
  };
  while (i < lines.length) {
    const line = lines[i];
    const h = /^(#{1,4})\s+(.*)$/.exec(line);
    if (h) {
      flush();
      const level = h[1].length;
      out.push(<Typography key={`h${i}`} sx={{
        fontSize: [20, 16, 14, 13][level - 1], fontWeight: level <= 2 ? 700 : 600,
        color: level <= 2 ? (tokens.navy ?? '#1F3864') : 'inherit',
        mt: level <= 2 ? 2 : 1.2, mb: 0.5,
        borderBottom: level === 2 ? `1px solid ${tokens.line}` : 'none',
      }}>{inline(h[2], `h${i}`)}</Typography>);
      i++;
      continue;
    }
    if (line.trim().startsWith('|')) {
      flush();
      const rows: string[] = [];
      while (i < lines.length && lines[i].trim().startsWith('|')) rows.push(lines[i++]);
      out.push(<Table key={`t${i}`} rows={rows} keyBase={`t${i}`} />);
      continue;
    }
    const li = /^\s*(?:[-*•]|\d+[.)])\s+(.*)$/.exec(line);
    if (li) {
      flush();
      const ordered = /^\s*\d/.test(line);
      const items: string[] = [];
      while (i < lines.length && /^\s*(?:[-*•]|\d+[.)])\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*(?:[-*•]|\d+[.)])\s+/, ''));
        i++;
      }
      out.push(<Box key={`l${i}`} component={ordered ? 'ol' : 'ul'} sx={{ my: 0.6, pl: 2.6 }}>
        {items.map((t, k) => <Box component="li" key={k} sx={{ fontSize: 12.8, lineHeight: 1.6 }}>
          {inline(t, `l${i}-${k}`)}</Box>)}
      </Box>);
      continue;
    }
    if (/^\s*>/.test(line)) {
      flush();
      out.push(<Box key={`q${i}`} sx={{ borderLeft: `3px solid ${tokens.line}`, pl: 1, my: 0.6,
        color: 'text.secondary', fontSize: 12.6 }}>{inline(line.replace(/^\s*>\s?/, ''), `q${i}`)}</Box>);
      i++;
      continue;
    }
    if (!line.trim()) { flush(); i++; continue; }
    para.push(line.replace(/\s{2,}$/, ''));
    i++;
  }
  flush();
  return <Box>{out.map((n, k) => <Fragment key={k}>{n}</Fragment>)}</Box>;
}
