import { useState } from 'react';
import { Alert, Button } from '@mui/material';
import { useQuery } from '@tanstack/react-query';
import { mailboxService, type MailboxStatus } from '../../services/mailboxService';
import MailboxDialog from './MailboxDialog';

const KEY = 'prism.mailbox.banner.until';
const DAYS = 30;

function snoozed(): boolean {
  try { return Number(localStorage.getItem(KEY) || 0) > Date.now(); } catch { return false; }
}

/**
 * One line on Today for a person who has not connected their mailbox: Connect, or
 * Not now (hidden for thirty days in this browser). Gone once connected, and never
 * shown when the reader is off or Google is not configured on the server, so it
 * never invites a click that cannot succeed. Also the "Reconnect" nudge when
 * Google has stopped accepting the person's token.
 */
export default function MailboxBanner() {
  const [open, setOpen] = useState(false);
  const [hidden, setHidden] = useState(snoozed());
  const { data } = useQuery({
    queryKey: ['mailbox', 'me'],
    queryFn: async () => { const r = await mailboxService.me(); if (!r.ok) throw new Error(r.error); return r.data as MailboxStatus; },
    staleTime: 60_000,
    retry: false,
  });
  if (!data || !data.enabled || !data.configured) return null;
  const reconnect = data.connected && data.status === 'reconnect';
  if (data.connected && !reconnect) return null;
  if (hidden && !reconnect) return null;
  const snooze = () => {
    try { localStorage.setItem(KEY, String(Date.now() + DAYS * 86_400_000)); } catch { /* private window */ }
    setHidden(true);
  };
  return (
    <>
      <Alert severity={reconnect ? 'warning' : 'info'} sx={{ mb: 1.5, fontSize: 12.75, alignItems: 'center' }}
        action={<>
          {!reconnect && <Button size="small" onClick={snooze} sx={{ textTransform: 'none' }}>Not now</Button>}
          <Button size="small" variant="contained" onClick={() => setOpen(true)} sx={{ textTransform: 'none', ml: 0.5 }}>
            {reconnect ? 'Reconnect' : 'Connect'}
          </Button>
        </>}>
        {reconnect
          ? 'Google no longer accepts PRISM’s permission for your mailbox. Reconnect to keep filing your conversations.'
          : 'PRISM can read your work mailbox (read-only) and file conversations with companies on their timelines.'}
      </Alert>
      <MailboxDialog open={open} onClose={() => setOpen(false)} />
    </>
  );
}
