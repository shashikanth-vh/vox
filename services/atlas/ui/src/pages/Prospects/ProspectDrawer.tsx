import { useMemo, useState } from 'react';
import {
  Alert, Autocomplete, Box, Button, Chip, Drawer, IconButton, TextField,
  Typography,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import PersonAddAlt1Icon from '@mui/icons-material/PersonAddAlt1';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import MenuItem from '@mui/material/MenuItem';
import { DrawerSection, FieldGrid, FieldShell, TextFld }
  from '../../components/common/Field';
import { tokens } from '../../theme';
import { apiErr } from '../../api/http';
import { prospectsService, type ProspectRow } from '../../services/prospectsService';

/**
 * The prospect drawer — the row's whole record on the right, the way the Deals
 * company drawer shows a client, instead of a cramped modal over the grid.
 *
 * One form, two authorities, enforced twice: status + remarks are the desk's
 * fields and stay editable for every role; every other section is curated
 * master data, editable in place for Admin/Management and read-only otherwise
 * — and the register enforces the same split field-by-field, so the disabled
 * inputs are a courtesy, not the security boundary.
 *
 * Saving sends ONLY what changed. A field cleared by the curator is sent as
 * null and clears on the register — the one thing the Excel import never does.
 */

export const STATUSES = ['uncontacted', 'contacted', 'interested', 'lead_created',
  'not_relevant'] as const;
export const STATUS_LABEL: Record<string, string> = {
  uncontacted: 'Uncontacted', contacted: 'Contacted', interested: 'Interested',
  lead_created: 'Lead created', not_relevant: 'Not relevant',
};
export const STATUS_COLOR: Record<string, 'default' | 'primary' | 'warning' | 'success'> = {
  uncontacted: 'default', contacted: 'primary', interested: 'warning',
  lead_created: 'success', not_relevant: 'default',
};

// The six ₹ Cr money fields, one list so the form and the save build agree.
export const MONEY_FIELDS: [keyof ProspectRow & string, string][] = [
  ['revenue_cr', 'Revenue'], ['net_profit_cr', 'Net profit (PAT)'],
  ['ebitda_cr', 'EBITDA'], ['total_funding_cr', 'Total funding'],
  ['latest_funding_cr', 'Latest round'], ['latest_valuation_cr', 'Latest valuation'],
];

export const splitList = (s: string) =>
  s.split(/[,;\n]+/).map((x) => x.trim()).filter(Boolean);

type Form = Record<string, string>;

const initForm = (p: ProspectRow): Form => {
  const out: Form = {
    name: p.name || '', domain: p.domain || '', cin: p.cin || '',
    state: p.state || '', city: p.city || '', country: p.country || '',
    founded_year: p.founded_year != null ? String(p.founded_year) : '',
    latest_funded_on: p.latest_funded_on || '',
    emails: (p.emails || []).join(', '), phones: (p.phones || []).join(', '),
    overview: p.overview || '', remarks: p.remarks || '', status: p.status,
  };
  MONEY_FIELDS.forEach(([k]) => {
    const v = p[k]; out[k] = v != null ? String(v) : '';
  });
  return out;
};

function DrawerBody({ p, canManage, canLead, tagOptions, onClose, onSaved,
  onCreateLead, onDelete }: {
  p: ProspectRow; canManage: boolean; canLead: boolean;
  tagOptions: { verticals: string[]; subs: string[] };
  onClose: () => void; onSaved: (msg: string) => void;
  onCreateLead?: (p: ProspectRow) => void; onDelete?: (p: ProspectRow) => void;
}) {
  const [f, setF] = useState<Form>(() => initForm(p));
  const [verts, setVerts] = useState<string[]>(p.verticals || []);
  const [subs, setSubs] = useState<string[]>(p.sub_sectors || []);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const set = (k: string) => (v: unknown) =>
    setF((prev) => ({ ...prev, [k]: String(v ?? '') }));

  const s = (k: string) => (f[k] || '').trim() || null;
  const n = (k: string) => {
    const t = (f[k] || '').trim();
    return t === '' || Number.isNaN(Number(t)) ? null : Number(t);
  };

  // The patch is CHANGES ONLY — nothing the person did not touch travels, so
  // the audit trail records the edit, not a restatement of the whole row.
  const patch = useMemo(() => {
    const out: Record<string, unknown> = {};
    if (f.status !== p.status) out.status = f.status;
    if (s('remarks') !== (p.remarks || null)) out.remarks = s('remarks');
    if (canManage) {
      if (f.name.trim() && f.name.trim() !== p.name) out.name = f.name.trim();
      (['domain', 'cin', 'state', 'city', 'country', 'overview',
        'latest_funded_on'] as const).forEach((k) => {
        if (s(k) !== (p[k] || null)) out[k] = s(k);
      });
      if (n('founded_year') !== (p.founded_year ?? null)) {
        out.founded_year = n('founded_year');
      }
      MONEY_FIELDS.forEach(([k]) => {
        if (n(k) !== (p[k] ?? null)) out[k] = n(k);
      });
      if (splitList(f.emails).join('|') !== (p.emails || []).join('|')) {
        out.emails = splitList(f.emails);
      }
      if (splitList(f.phones).join('|') !== (p.phones || []).join('|')) {
        out.phones = splitList(f.phones);
      }
      if (verts.join('|') !== (p.verticals || []).join('|')) out.verticals = verts;
      if (subs.join('|') !== (p.sub_sectors || []).join('|')) out.sub_sectors = subs;
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [f, verts, subs, p, canManage]);
  const dirty = Object.keys(patch).length > 0;

  const save = async () => {
    if (!dirty) return;
    setErr(''); setBusy(true);
    try {
      // Non-managers only ever stage desk fields (the rest are disabled), and
      // the two service calls hit the same PATCH — the split is documentation.
      await (canManage
        ? prospectsService.updateMaster(p.id, patch)
        : prospectsService.update(p.id, patch as { status?: string; remarks?: string }));
      onSaved(`${p.name} updated.`);
    } catch (e: any) { setErr(apiErr(e, 'save the prospect')); }
    finally { setBusy(false); }
  };

  const ro = !canManage;
  const leadCount = (p.lead_ids || []).length;

  return (
    <>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.2, p: '9px 16px',
        bgcolor: '#F1F3F5', borderBottom: 1, borderColor: 'divider', flexShrink: 0 }}>
        <Typography sx={{ fontSize: 15.6, fontWeight: 700, flex: 1 }}>
          {p.name}
          <Typography component="span" sx={{ fontSize: 12, color: tokens.muted,
            fontWeight: 500, ml: 1 }}>{p.prospect_no}</Typography>
        </Typography>
        <Chip size="small" color={STATUS_COLOR[p.status] || 'default'}
          label={STATUS_LABEL[p.status] || p.status} sx={{ fontSize: 10.8, height: 21 }} />
        <IconButton size="small" onClick={onClose}><CloseIcon fontSize="small" /></IconButton>
      </Box>

      <Box sx={{ flex: 1, minHeight: 0, overflowY: 'auto', p: 2 }}>
        <DrawerSection title="Status & remarks">
          <FieldGrid cols={1}>
            <FieldShell label="Status">
              <TextField select fullWidth size="small" value={f.status}
                disabled={busy} onChange={(e) => set('status')(e.target.value)}>
                {STATUSES.map((st) => (
                  <MenuItem key={st} value={st}>{STATUS_LABEL[st]}</MenuItem>))}
              </TextField>
            </FieldShell>
            <TextFld label="Remarks" value={f.remarks} onChange={set('remarks')}
              multiline minRows={2} disabled={busy}
              placeholder="Met at REI Expo; CFO open to a WC discussion after Diwali…" />
          </FieldGrid>
        </DrawerSection>

        <DrawerSection title="Profile">
          <FieldGrid cols={2}>
            <TextFld label="Company name" value={f.name} onChange={set('name')}
              disabled={ro || busy} required />
            <TextFld label="Domain (website)" value={f.domain} onChange={set('domain')}
              disabled={ro || busy} />
            <TextFld label="CIN" value={f.cin} onChange={set('cin')}
              disabled={ro || busy} />
            <TextFld label="Founded year" value={f.founded_year}
              onChange={set('founded_year')} disabled={ro || busy} />
            <TextFld label="State" value={f.state} onChange={set('state')}
              disabled={ro || busy} />
            <TextFld label="City" value={f.city} onChange={set('city')}
              disabled={ro || busy} />
            <TextFld label="Country" value={f.country} onChange={set('country')}
              disabled={ro || busy} />
          </FieldGrid>
        </DrawerSection>

        <DrawerSection title="Sectors">
          {ro ? (
            <Box sx={{ display: 'flex', gap: 0.5, flexWrap: 'wrap' }}>
              {verts.map((v) => <Chip key={v} size="small" color="primary"
                variant="outlined" label={v} sx={{ fontSize: 10.8, height: 21 }} />)}
              {subs.map((x) => <Chip key={x} size="small" variant="outlined"
                label={x} sx={{ fontSize: 10.8, height: 21 }} />)}
              {!verts.length && !subs.length && (
                <Typography sx={{ fontSize: 12, color: tokens.muted }}>
                  No sectors tagged.</Typography>)}
            </Box>
          ) : (
            <FieldGrid cols={1}>
              <FieldShell label="Verticals">
                <Autocomplete multiple freeSolo size="small"
                  options={tagOptions.verticals}
                  value={verts} onChange={(_, v) => setVerts(v as string[])}
                  disabled={busy}
                  renderInput={(pr) => <TextField {...pr} placeholder="Solar, EV, ESS…" />} />
              </FieldShell>
              <FieldShell label="Sub-sectors">
                <Autocomplete multiple freeSolo size="small"
                  options={tagOptions.subs}
                  value={subs} onChange={(_, v) => setSubs(v as string[])}
                  disabled={busy}
                  renderInput={(pr) => <TextField {...pr} placeholder="OEM, EPC, Developer…" />} />
              </FieldShell>
            </FieldGrid>
          )}
        </DrawerSection>

        <DrawerSection title="Financials (₹ Cr)">
          <FieldGrid cols={2}>
            {MONEY_FIELDS.map(([k, label]) => (
              <TextFld key={k} label={label} value={f[k]} onChange={set(k)}
                disabled={ro || busy} />))}
            <TextFld label="Latest funded date" type="date"
              value={f.latest_funded_on} onChange={set('latest_funded_on')}
              disabled={ro || busy} />
          </FieldGrid>
        </DrawerSection>

        <DrawerSection title="Contacts">
          <FieldGrid cols={1}>
            <TextFld label="Emails (comma-separated)" value={f.emails}
              onChange={set('emails')} multiline disabled={ro || busy} />
            <TextFld label="Phones (comma-separated)" value={f.phones}
              onChange={set('phones')} multiline disabled={ro || busy} />
          </FieldGrid>
        </DrawerSection>

        <DrawerSection title="About the company">
          <TextFld label="Overview" value={f.overview} onChange={set('overview')}
            multiline minRows={3} disabled={ro || busy} />
        </DrawerSection>

        <DrawerSection title="Leads">
          <Typography sx={{ fontSize: 12.4 }}>
            {leadCount
              ? `${leadCount} lead(s) created from this prospect — a company can `
                + 'carry several asks, and they all link to the same client master.'
              : 'No leads yet — create one when the company shows interest.'}
          </Typography>
        </DrawerSection>

        {err && <Alert severity="warning" sx={{ fontSize: 12.4 }}>{err}</Alert>}
      </Box>

      <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', p: '10px 16px',
        borderTop: 1, borderColor: 'divider', flexShrink: 0 }}>
        {canLead && onCreateLead && (
          <Button size="small" startIcon={<PersonAddAlt1Icon />} disabled={busy}
            onClick={() => onCreateLead(p)}>Create lead</Button>)}
        {onDelete && (
          <Button size="small" color="error" startIcon={<DeleteOutlineIcon />}
            disabled={busy} onClick={() => onDelete(p)}>Delete</Button>)}
        <Box sx={{ flex: 1 }} />
        <Button size="small" onClick={onClose} disabled={busy}>Close</Button>
        <Button size="small" variant="contained" onClick={save}
          disabled={busy || !dirty || !f.name.trim()}>Save changes</Button>
      </Box>
    </>
  );
}

export default function ProspectDrawer({ prospect, canManage, canLead, tagOptions,
  onClose, onSaved, onCreateLead, onDelete }: {
  prospect: ProspectRow | null; canManage: boolean; canLead: boolean;
  tagOptions: { verticals: string[]; subs: string[] };
  onClose: () => void; onSaved: (msg: string) => void;
  onCreateLead?: (p: ProspectRow) => void; onDelete?: (p: ProspectRow) => void;
}) {
  return (
    <Drawer anchor="right" open={!!prospect} onClose={onClose}
      PaperProps={{ sx: { width: 560, maxWidth: '100vw', height: '100%',
        display: 'flex', flexDirection: 'column' } }}>
      {prospect && (
        // Keyed by row + version so a save (which refreshes the universe) and a
        // row switch both restart the form from the register's current truth.
        <DrawerBody key={`${prospect.id}:${(prospect as any).version ?? ''}`}
          p={prospect} canManage={canManage} canLead={canLead}
          tagOptions={tagOptions} onClose={onClose}
          onSaved={onSaved} onCreateLead={onCreateLead} onDelete={onDelete} />
      )}
    </Drawer>
  );
}
