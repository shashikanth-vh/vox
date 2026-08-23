/**
 * The registry-driven report renderer — the Phase 0 exit criterion made real:
 * it renders whatever blocks the registry defines, so adding a field (or a whole
 * use case) in schema_registry/vX.json requires ZERO changes here.
 *
 * Layout follows the spec's review screen: the common facts, then one card per
 * detected use case (asset monetisation branching on party role), the tappable
 * opportunity score with its override semantics, and the heavy context —
 * sector/subsector with the per-subsector canonical data points, judgement fields,
 * data-quality flags — folded under "Additional details".
 *
 * Every field wears its confidence dot (green solid = high, amber = medium,
 * hollow amber = low, dashed = n/a). Tapping a flagged field, or editing it,
 * confirms it — the parent owns the confirmed set and the gated Approve.
 */

import { Box, Chip, MenuItem, TextField, Typography } from '@mui/material';
import { useState } from 'react';
import { card, chip, input, label as lbl, microHeading, vx } from '../vocxStyles';
import type { VoxCell, VoxRegistry, VoxReport } from '../../../services/voxService';
import { amFieldVisible } from '../../../services/voxService';

const JUDGEMENT = new Set(['opportunity_assessment', 'opportunity_score_override_reason',
  'competitive_intelligence']);
const HIDDEN_IN_MAIN = new Set(['sector', 'subsector', 'attendees_counterparty',
  'opportunity_assessment', 'opportunity_score', 'opportunity_score_override_reason',
  'competitive_intelligence', 'data_quality_flags']);

export function ConfidenceDot({ conf }: { conf?: string }) {
  const sx: any = { display: 'inline-block', width: 9, height: 9, borderRadius: '50%',
    ml: 0.8, verticalAlign: '1px' };
  if (conf === 'high') sx.bgcolor = vx.grn;
  else if (conf === 'medium') sx.bgcolor = vx.amber;
  else if (conf === 'low') { sx.border = `2px solid ${vx.amber}`; sx.width = 7; sx.height = 7; }
  else { sx.border = `1.5px dashed ${vx.mut}`; sx.width = 7; sx.height = 7; }
  return <Box component="span" sx={sx} title={`confidence: ${conf || 'n/a'}`} />;
}

interface FieldProps {
  def: any;
  cell: VoxCell | undefined;
  path: string;
  readOnly: boolean;
  flash: boolean;
  onCell: (path: string, cell: VoxCell) => void;
  onConfirm: (path: string) => void;
}

function listToText(v: any): string {
  return Array.isArray(v) ? v.map((x) => (typeof x === 'string' ? x : JSON.stringify(x))).join('\n') : (v ?? '');
}

function FieldRow({ def, cell, path, readOnly, flash, onCell, onConfirm }: FieldProps) {
  const value = cell?.value;
  const set = (v: any) => {
    onCell(path, { ...(cell || { confidence: 'high' }), value: v } as VoxCell);
    onConfirm(path);
  };
  const frame = {
    ...input,
    ...(flash ? { '& .MuiInputBase-root': { ...(input as any)['& .MuiInputBase-root'],
      border: `1px solid ${vx.amber}`, boxShadow: `0 0 0 1px ${vx.amber}` } } : {}),
  };
  const control = def.control;

  let body: React.ReactNode;
  if (control === 'dropdown') {
    const opts = (def.options || []).map((o: any) =>
      typeof o === 'string' ? { value: o, label: o } : o);
    body = (
      <TextField select fullWidth size="small" sx={frame} disabled={readOnly}
        value={value ?? ''} onChange={(e) => set(e.target.value || null)}>
        <MenuItem value="">—</MenuItem>
        {opts.map((o: any) => <MenuItem key={o.value} value={o.value}>{o.label}</MenuItem>)}
        {value && !opts.some((o: any) => o.value === value) && (
          <MenuItem value={value}>{value}</MenuItem>)}
      </TextField>
    );
  } else if (control === 'chips') {
    const chosen: string[] = Array.isArray(value) ? value : [];
    const closed: string[] = def.closed_set || [];
    body = (
      <Box>
        {[...closed, ...chosen.filter((c) => !closed.includes(c))].map((c) => (
          <Chip key={c} label={c} sx={{ ...chip(chosen.includes(c)), mr: 0.7, mb: 0.7 }}
            onClick={readOnly ? undefined : () =>
              set(chosen.includes(c) ? chosen.filter((x) => x !== c) : [...chosen, c])} />
        ))}
        {!readOnly && def.allow_free_text && (
          <Chip label="+ add" sx={{ ...chip(false, true), mr: 0.7, mb: 0.7 }}
            onClick={() => {
              const extra = window.prompt('Add a component');
              if (extra?.trim()) set([...chosen, extra.trim()]);
            }} />
        )}
      </Box>
    );
  } else if (control === 'action_items') {
    const items: any[] = Array.isArray(value) ? value : [];
    body = (
      <Box>
        {items.map((it, i) => (
          <Box key={i} sx={{ display: 'flex', gap: 0.8, mb: 0.7, flexWrap: 'wrap' }}>
            <TextField size="small" sx={{ ...input, flex: '1 1 100%' }} disabled={readOnly}
              value={it.action || ''} placeholder="Action"
              onChange={(e) => set(items.map((x, j) => (j === i ? { ...x, action: e.target.value } : x)))} />
            <TextField size="small" sx={{ ...input, flex: '1 1 120px' }} disabled={readOnly}
              value={it.owner || ''} placeholder="Owner"
              onChange={(e) => set(items.map((x, j) => (j === i ? { ...x, owner: e.target.value } : x)))} />
            <TextField size="small" type="date" sx={{ ...input, width: 150 }} disabled={readOnly}
              value={it.deadline || ''}
              onChange={(e) => set(items.map((x, j) => (j === i ? { ...x, deadline: e.target.value || null } : x)))} />
          </Box>
        ))}
        {!readOnly && (
          <Chip label="+ action" sx={chip(false, true)}
            onClick={() => set([...items, { action: '', owner: null, deadline: null }])} />
        )}
      </Box>
    );
  } else if (control === 'list') {
    body = (
      <TextField fullWidth multiline minRows={Math.min(3, Math.max(1, (value || []).length))}
        size="small" sx={frame} disabled={readOnly}
        value={listToText(value)} placeholder="One per line"
        onChange={(e) => set(e.target.value.split('\n').map((s) => s.trim()).filter(Boolean))} />
    );
  } else if (control === 'number') {
    body = (
      <TextField fullWidth size="small" type="number" sx={frame} disabled={readOnly}
        value={value ?? ''}
        onChange={(e) => set(e.target.value === '' ? null : Number(e.target.value))} />
    );
  } else if (control === 'date') {
    body = (
      <TextField fullWidth size="small" type="date" sx={frame} disabled={readOnly}
        value={value ?? ''}
        onChange={(e) => set(e.target.value || null)} />
    );
  } else {
    body = (
      <TextField fullWidth size="small" multiline={control === 'textarea'}
        minRows={control === 'textarea' ? 2 : undefined} sx={frame} disabled={readOnly}
        value={value ?? ''}
        onChange={(e) => set(e.target.value || null)} />
    );
  }

  return (
    <Box id={`vox-${path}`} sx={{ mb: 1.4 }} onClick={() => !readOnly && onConfirm(path)}>
      <Typography component="span" sx={lbl}>
        {def.label}
        <ConfidenceDot conf={cell?.confidence} />
      </Typography>
      {body}
    </Box>
  );
}

export default function RegistryReport({ registry, report, readOnly, confirmed, flashPath,
  onCell, onConfirm }: {
  registry: VoxRegistry;
  report: VoxReport;
  readOnly: boolean;
  confirmed: Set<string>;
  flashPath?: string | null;
  onCell: (path: string, cell: VoxCell) => void;
  onConfirm: (path: string) => void;
}) {
  const [more, setMore] = useState(false);
  const common = (report.common || {}) as Record<string, VoxCell>;
  const detected = report.detected_use_cases || [];
  const score = common.opportunity_score;
  const reason = common.opportunity_score_override_reason;
  const subsector = common.subsector?.value as string | null;
  const canonicals = (subsector && registry.subsector_canonicals[subsector]) || [];
  const flags: string[] = (common.data_quality_flags?.value as string[]) || [];

  const needsFlash = (p: string) => flashPath === p && !confirmed.has(p);

  const renderBlock = (blockKey: string, defs: any[], title: string, note?: string) => {
    const cells = (blockKey === 'common' ? common : (report as any)[blockKey]) || {};
    const partyRole = blockKey === 'asset_monetisation'
      ? (cells.party_role?.value as string | null) : null;
    const visible = defs.filter((d) =>
      (blockKey !== 'common' || !HIDDEN_IN_MAIN.has(d.key))
      && (blockKey !== 'asset_monetisation' || amFieldVisible(d, partyRole)));
    if (!visible.length) return null;
    return (
      <Box key={blockKey} sx={card}>
        <Typography sx={microHeading}>{title}</Typography>
        {note && <Typography sx={{ fontSize: 12, color: vx.mut, mb: 1 }}>{note}</Typography>}
        {visible.map((def) => (
          <FieldRow key={def.key} def={def} cell={cells[def.key]}
            path={`${blockKey}.${def.key}`} readOnly={readOnly}
            flash={needsFlash(`${blockKey}.${def.key}`)}
            onCell={onCell} onConfirm={onConfirm} />
        ))}
      </Box>
    );
  };

  return (
    <Box>
      {flags.length > 0 && (
        <Box sx={{ ...card, borderColor: '#4A3D1D' }}>
          <Typography sx={{ ...microHeading, color: vx.amberInk }}>Data quality</Typography>
          {flags.map((f) => (
            <Typography key={f} sx={{ fontSize: 13, color: vx.amberInk }}>• {f}</Typography>))}
        </Box>
      )}

      {renderBlock('common', registry.common, 'Common')}
      {detected.map((uc) => {
        const block = registry.blocks[uc];
        if (!block) return null;
        return block.fields.length
          ? renderBlock(uc, block.fields, block.label, block.ui_note)
          : (
            <Box key={uc} sx={card}>
              <Typography sx={microHeading}>{block.label}</Typography>
              <Typography sx={{ fontSize: 12.5, color: vx.mut }}>
                Captured with the common field set — dedicated fields follow with volume.
              </Typography>
            </Box>
          );
      })}

      {/* The opportunity score — AI-seeded, human-owned. Never a required item. */}
      <Box sx={card}>
        <Typography sx={microHeading}>Opportunity score — AI seeded, yours to own</Typography>
        <Box sx={{ display: 'flex', gap: 0.8, mb: 0.8 }}>
          {[1, 2, 3, 4, 5].map((n) => {
            const on = score?.value === n;
            return (
              <Box key={n} onClick={() => {
                if (readOnly) return;
                onCell('common.opportunity_score',
                  { value: score?.value === n ? null : n, confidence: 'n/a', user_override: true });
              }}
                sx={{ flex: 1, textAlign: 'center', py: 1, borderRadius: '10px',
                  cursor: readOnly ? 'default' : 'pointer', fontWeight: 700,
                  bgcolor: on ? vx.grn : vx.card2, color: on ? vx.onGrn : vx.ink,
                  border: `1px solid ${on ? vx.grn : vx.line}` }}>{n}</Box>
            );
          })}
        </Box>
        <Typography sx={{ fontSize: 12.5, color: vx.mut }}>
          {score?.user_override
            ? 'You set this score — marked user-override, confidence n/a.'
            : score?.value
              ? `AI suggested ${score.value} (${score.confidence}). Tap a cell to override.`
              : 'The AI left this null — no evaluative language heard. Tap to set it.'}
        </Typography>
        {score?.user_override && !readOnly && (
          <TextField fullWidth size="small" sx={{ ...input, mt: 1 }}
            placeholder="Reason for change (optional)…"
            value={reason?.value ?? ''}
            onChange={(e) => onCell('common.opportunity_score_override_reason',
              { value: e.target.value || null, confidence: 'n/a' })} />
        )}
      </Box>

      {/* Heavy context folds away, exactly as the spec draws it. */}
      <Box sx={{ ...card, cursor: 'pointer', py: 1.2 }} onClick={() => setMore((m) => !m)}>
        <Typography sx={{ ...microHeading, mb: 0 }}>Additional details {more ? '▾' : '▸'}</Typography>
      </Box>
      {more && (
        <Box sx={card}>
          {['sector', 'subsector', 'attendees_counterparty'].map((key) => {
            const def = registry.common.find((d: any) => d.key === key);
            if (!def) return null;
            const opts = key === 'sector'
              ? Object.keys(registry.taxonomy)
              : key === 'subsector'
                ? (registry.taxonomy[common.sector?.value as string] || [])
                : null;
            return (
              <FieldRow key={key}
                def={opts ? { ...def, control: 'dropdown', options: opts } : def}
                cell={common[key]} path={`common.${key}`} readOnly={readOnly}
                flash={needsFlash(`common.${key}`)} onCell={onCell} onConfirm={onConfirm} />
            );
          })}
          {canonicals.length > 0 && (
            <>
              <Typography sx={{ ...microHeading, mt: 1 }}>{subsector} — canonical data points</Typography>
              {canonicals.map((def: any) => (
                <FieldRow key={def.key} def={def}
                  cell={(report.subsector_details || {})[def.key]}
                  path={`subsector_details.${def.key}`} readOnly={readOnly}
                  flash={needsFlash(`subsector_details.${def.key}`)}
                  onCell={onCell} onConfirm={onConfirm} />
              ))}
            </>
          )}
          {[...JUDGEMENT].map((key) => {
            const def = registry.common.find((d: any) => d.key === key);
            return def && key !== 'opportunity_score_override_reason' ? (
              <FieldRow key={key} def={def} cell={common[key]} path={`common.${key}`}
                readOnly={readOnly} flash={false} onCell={onCell} onConfirm={onConfirm} />
            ) : null;
          })}
        </Box>
      )}
    </Box>
  );
}
