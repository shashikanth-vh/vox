import { useState } from 'react';
import { Box, Typography } from '@mui/material';
import type { Interaction } from '../../services/interactionService';
import { tokens } from '../../theme';

/**
 * One interaction on a drawer timeline, in two altitudes:
 *
 *   collapsed — the scan line (date · type · who · with whom), a source badge, the
 *               summary, the "Next:" pill, and — when the row carries depth — small
 *               chips saying so (💡 intel · ☑ steps · 🎙 transcript). The chips are the
 *               point: you can SEE where the intelligence lives before opening anything.
 *   expanded  — tap the row: full notes, outcome, the key-intel panel, next steps,
 *               attendees / location / next meeting. The transcript stays behind its
 *               own toggle even here — a voice note's transcript would drown the card.
 *
 * Used by both the company drawer and the lead drawer so the two timelines cannot
 * drift apart.
 */

const SOURCE_BADGE: Record<string, [string, string]> = {
  VOX: ['🎙 VOX', '#E8F4F3'], Email: ['✉ Email', '#EDF2FB'], System: ['⚙ System', '#F3F4F6'],
};

// key_intel arrives in whatever shape its writer used: VocX a dict, its reports a
// bullet list, older rows a plain string. Flatten every shape to label/text lines.
function intelLines(ki: any): { label?: string; text: string }[] {
  if (!ki) return [];
  if (typeof ki === 'string') return ki.trim() ? [{ text: ki }] : [];
  if (Array.isArray(ki)) {
    return ki.filter(Boolean).map((x) =>
      typeof x === 'string' ? { text: x } : { label: x?.label || x?.key, text: String(x?.text ?? x?.value ?? JSON.stringify(x)) });
  }
  return Object.entries(ki)
    .filter(([, v]) => v != null && String(v).trim() !== '')
    .map(([k, v]) => ({ label: k.replace(/_/g, ' '), text: Array.isArray(v) ? v.join('; ') : String(v) }));
}

const stepText = (s: any): string =>
  typeof s === 'string' ? s : String(s?.text ?? s?.step ?? s?.action ?? JSON.stringify(s));

function Chip({ children, tone = '#F3F4F6', color = tokens.muted }:
  { children: React.ReactNode; tone?: string; color?: string }) {
  return (
    <Box component="span" sx={{ px: 0.8, py: 0.15, borderRadius: 99, bgcolor: tone, color,
      fontSize: 10, fontWeight: 700, whiteSpace: 'nowrap' }}>{children}</Box>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <Box sx={{ mt: 0.8 }}>
      <Typography sx={{ fontSize: 10, color: tokens.muted, fontWeight: 700, letterSpacing: 0.4, textTransform: 'uppercase' }}>{label}</Typography>
      <Box sx={{ fontSize: 12.3, mt: 0.2 }}>{children}</Box>
    </Box>
  );
}

// `leadBadge` off on the lead drawer — there every row is lead-phase, so the badge
// would be noise; on the company drawer it marks the inherited pre-conversion story.
export default function InteractionRow({ i, leadBadge = true }: { i: Interaction; leadBadge?: boolean }) {
  const [open, setOpen] = useState(false);
  const [showTranscript, setShowTranscript] = useState(false);
  const intel = intelLines(i.keyIntel);
  const hasDepth = !!(intel.length || i.fullNotes || i.outcome || i.nextSteps?.length
    || i.transcript || i.attendees?.length || i.location || i.nextMeetingDate);
  const badge = i.source ? SOURCE_BADGE[i.source] : undefined;

  return (
    <Box sx={{ mb: 1.2, cursor: hasDepth ? 'pointer' : 'default' }}
      onClick={hasDepth ? () => setOpen((v) => !v) : undefined}>
      <Typography sx={{ fontSize: 11, color: tokens.muted, fontWeight: 600, display: 'flex',
        alignItems: 'center', gap: 0.7, flexWrap: 'wrap' }}>
        <span>
          {i.occurredAt} · {i.interactionType} · by {i.person || '—'}
          {i.lenderName ? ` · ${i.lenderName}` : ''}{i.direction ? ` · ${i.direction}` : ''}
        </span>
        {badge && <Chip tone={badge[1]} color="#155E63">{badge[0]}</Chip>}
        {leadBadge && i.refType === 'Lead' && <Chip tone="#FFF3E0" color="#9A6A00">LEAD PHASE</Chip>}
        {!open && intel.length > 0 && <Chip tone="#FdF6E3" color="#8A6D00">💡 intel · {intel.length}</Chip>}
        {!open && (i.nextSteps?.length ?? 0) > 0 && <Chip>☑ steps · {i.nextSteps!.length}</Chip>}
        {!open && i.transcript && !badge && <Chip>🎙 transcript</Chip>}
        {hasDepth && (
          <Box component="span" sx={{ ml: 'auto', color: tokens.navy, fontSize: 10.5, fontWeight: 700 }}>
            {open ? '▴ less' : '▾ more'}
          </Box>
        )}
      </Typography>
      <Typography sx={{ fontSize: 12.5, whiteSpace: 'pre-wrap', mt: 0.3 }}>{i.notes}</Typography>

      {open && (
        <Box onClick={(e) => e.stopPropagation()}
          sx={{ mt: 0.6, px: 1.2, py: 0.9, bgcolor: '#FAFBFC', border: `1px solid ${tokens.line}`, borderRadius: 1.5, cursor: 'default' }}>
          {i.fullNotes && <Field label="Notes"><span style={{ whiteSpace: 'pre-wrap' }}>{i.fullNotes}</span></Field>}
          {i.outcome && <Field label="Outcome"><span style={{ whiteSpace: 'pre-wrap' }}>{i.outcome}</span></Field>}
          {intel.length > 0 && (
            <Field label="Key intelligence">
              {intel.map((x, k) => (
                <Typography key={k} sx={{ fontSize: 12.3, lineHeight: 1.7 }}>
                  • {x.label && <b style={{ textTransform: 'capitalize' }}>{x.label}: </b>}{x.text}
                </Typography>
              ))}
            </Field>
          )}
          {(i.nextSteps?.length ?? 0) > 0 && (
            <Field label="Next steps">
              {i.nextSteps!.map((s, k) => (
                <Typography key={k} sx={{ fontSize: 12.3, lineHeight: 1.7 }}>☐ {stepText(s)}</Typography>
              ))}
            </Field>
          )}
          {i.attendees?.length ? (
            <Field label="Attendees">{i.attendees.map((a: any) => typeof a === 'string' ? a : (a?.name || '')).filter(Boolean).join(', ')}</Field>
          ) : null}
          {i.location && <Field label="Location">{i.location}</Field>}
          {i.nextMeetingDate && <Field label="Next meeting">{i.nextMeetingDate}</Field>}
          {i.transcript && (
            <Box sx={{ mt: 0.9 }}>
              <Typography onClick={() => setShowTranscript((v) => !v)}
                sx={{ fontSize: 11, color: tokens.navy, fontWeight: 700, cursor: 'pointer', display: 'inline-block' }}>
                {showTranscript ? '▴ Hide transcript' : '▾ View transcript'}
              </Typography>
              {showTranscript && (
                <Typography sx={{ fontSize: 11.8, whiteSpace: 'pre-wrap', mt: 0.5, color: '#3A4A54',
                  maxHeight: 260, overflow: 'auto', bgcolor: '#fff', border: `1px solid ${tokens.line}`, borderRadius: 1, p: 1 }}>
                  {i.transcript}
                </Typography>
              )}
            </Box>
          )}
        </Box>
      )}

      {i.nextAction && (
        <Typography sx={{ fontSize: 11.5, color: tokens.navy, bgcolor: '#EEF4F3', borderRadius: 1,
          px: 1, py: 0.4, mt: 0.5, display: 'inline-block' }}>
          Next: {i.nextAction}{i.nextActionDate ? ` by ${i.nextActionDate}` : ''}
        </Typography>
      )}
    </Box>
  );
}
