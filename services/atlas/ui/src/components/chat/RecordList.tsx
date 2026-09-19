import { useState } from 'react';
import { Box, Link, Typography } from '@mui/material';
import type { ResultTable as Result } from './chatClient';
import { tokens } from '../../theme';

/**
 * The record results Chitti returns, read down instead of across.
 *
 * A column-per-field grid needs the better part of 850px. Pushed into the 384px panel —
 * which is the only place Chitti lives — it produced the worst of both: a horizontal
 * scrollbar INSIDE a vertical one, columns sliced off mid-word ("Sta…"), a company name
 * broken across lines, and a rows-per-page control wider than the panel itself.
 * Everything the answer was about ended up off-screen behind a scrollbar nobody thinks
 * to drag.
 *
 * So each record is a small card of label/value pairs instead. Nothing is dropped — every
 * column a table would have shown is here, just stacked, which is the one axis a chat
 * surface already scrolls. Long rows are previewed rather than truncated: the rest are a
 * click away, in the panel, not somewhere else.
 */

/** Records shown before the list asks whether you really want the rest. */
const PREVIEW = 5;

export default function RecordList({ result }: { result: Result }) {
  const [all, setAll] = useState(false);
  // Same contract as ResultTable: a 'Group code' column carries the deal link itself,
  // and without one a row's `deal` becomes a leading field of its own.
  const codeColumn = result.columns.indexOf('Group code');
  const leadingDeal = codeColumn < 0 && result.rows.some(row => row.deal);
  const headline = codeColumn >= 0 ? codeColumn : 0;

  const dealLink = (code: string) => (
    <Link
      href={`${import.meta.env.BASE_URL}deals?company=${encodeURIComponent(code)}`}
      aria-label={`Open deal ${code}`}
      sx={{ fontSize: 12.5, fontWeight: 700, overflowWrap: 'anywhere' }}
    >{code}</Link>
  );

  const shown = all ? result.rows : result.rows.slice(0, PREVIEW);
  const hidden = result.rows.length - shown.length;

  return (
    <Box mt={1.25}>
      <Typography sx={{ fontSize: 11.5, fontWeight: 700, color: tokens.ink }}>
        {result.title} ({result.total})
      </Typography>
      {result.partial && (
        <Typography sx={{ fontSize: 10.5, color: tokens.muted }}>
          These are the records retrieved so far; more may match.
        </Typography>
      )}
      {result.rows.length < result.total && (
        <Typography sx={{ fontSize: 10.5, color: tokens.muted }}>
          Showing {result.rows.length} of {result.total}. Narrow your question to see the rest.
        </Typography>
      )}

      <Box component="ul" aria-label={result.title}
        sx={{ listStyle: 'none', m: 0, mt: 0.75, p: 0, display: 'grid', gap: 0.75 }}>
        {shown.map((row, index) => {
          const title = row.values[headline] || row.deal || `Record ${index + 1}`;
          // A query can come back with the same column twice (two 'Group code's is the
          // common one). Side by side in a table that reads as a quirk; stacked as
          // label/value it reads as a contradiction — so a repeated label with nothing
          // in it is dropped. A repeat that HAS a value is kept: that is real data.
          const seen = new Set<string>(result.columns[headline] ? [result.columns[headline]] : []);
          const fields = [
            ...(leadingDeal && row.deal ? [{ label: 'Deal', node: dealLink(row.deal) }] : []),
            ...result.columns.flatMap((label, col) => {
              if (col === headline) return [];
              const value = row.values[col];
              if (!value && seen.has(label)) return [];
              seen.add(label);
              return [{
                label,
                node: (
                  <Typography component="span" sx={{ fontSize: 11.5, color: tokens.ink,
                    overflowWrap: 'anywhere', whiteSpace: 'pre-wrap' }}>
                    {value || '—'}
                  </Typography>
                ),
              }];
            }),
          ];
          return (
            <Box component="li" key={index}
              sx={{ border: `1px solid ${tokens.line}`, borderRadius: '8px',
                    bgcolor: tokens.paper, px: 1, py: 0.75 }}>
              {codeColumn >= 0 && row.deal
                ? dealLink(row.deal)
                : <Typography sx={{ fontSize: 12.5, fontWeight: 700, color: tokens.ink,
                    overflowWrap: 'anywhere' }}>{title}</Typography>}
              {fields.map(({ label, node }, i) => (
                // Label in a fixed gutter, value taking the rest and wrapping — so a long
                // company name pushes the card DOWN rather than off the side.
                <Box key={i} sx={{ display: 'flex', gap: 0.75, mt: 0.4 }}>
                  <Typography component="span" sx={{ fontSize: 10.5, color: tokens.muted,
                    width: 96, flexShrink: 0, lineHeight: 1.45, overflowWrap: 'anywhere' }}>
                    {label}
                  </Typography>
                  <Box sx={{ flex: 1, minWidth: 0, lineHeight: 1.45 }}>{node}</Box>
                </Box>
              ))}
            </Box>
          );
        })}
      </Box>

      {hidden > 0 && (
        <Typography component="button" type="button" onClick={() => setAll(true)}
          sx={{ mt: 0.75, fontSize: 11, fontWeight: 600, color: tokens.teal, background: 'none',
                border: 0, p: 0, cursor: 'pointer' }}>
          Show {hidden} more record{hidden === 1 ? '' : 's'}
        </Typography>
      )}
    </Box>
  );
}
