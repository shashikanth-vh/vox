import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert, Badge, Box, CircularProgress, IconButton, Paper, Stack, TextField, Tooltip, Typography,
} from '@mui/material';
import ChatBubbleOutlineIcon from '@mui/icons-material/ChatBubbleOutline';
import CloseIcon from '@mui/icons-material/Close';
import MinimizeIcon from '@mui/icons-material/Minimize';
import OpenInFullIcon from '@mui/icons-material/OpenInFull';
import DragIndicatorIcon from '@mui/icons-material/DragIndicator';
import EditIcon from '@mui/icons-material/Edit';
import SendIcon from '@mui/icons-material/Send';
import StopCircleIcon from '@mui/icons-material/StopCircle';
import RestartAltIcon from '@mui/icons-material/RestartAlt';
import { useDraggable, type Point } from '../common/useDraggable';
import { useChitti } from './ChittiProvider';
import RecordList from './RecordList';
import { tokens } from '../../theme';

/**
 * Chitti — the whole of it. There is no Chitti tab and no Chitti page: this floating
 * bubble IS the assistant, on every view in the app.
 *
 * Being a bubble rather than a page is the point, and it is not modal either: the grid
 * behind it stays scrollable and clickable, so an RM can read an answer with the deal it
 * is about still on screen. That only works if the panel can be moved off whatever it
 * happens to be covering, hence drag-to-place on both the bubble and the panel header,
 * each remembering where it was parked. The conversation lives in ChittiProvider at the
 * layout root, so it keeps streaming while the user navigates between pages and survives
 * the panel being closed and reopened.
 *
 * On a phone a 384px window that can be dragged half off a 390px screen is worse than no
 * window, so the panel docks as a bottom sheet above the tab bar and only the bubble moves.
 */

const FAB = 56;
const PANEL_W = 384;
const PANEL_H = 560;
const HEADER_H = 44;
const MOBILE_MAX = 760;
const BOTTOM_NAV = 56;          // the phone tab bar the bubble must not sit on top of

export default function ChittiWidget() {
  const { open, setOpen, unread } = useChitti();
  const [mobile, setMobile] = useState(
    () => typeof window !== 'undefined' && window.innerWidth <= MOBILE_MAX);

  useEffect(() => {
    const onResize = () => setMobile(window.innerWidth <= MOBILE_MAX);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  const fabSize = useCallback(() => ({ w: FAB, h: FAB }), []);
  const fabInitial = useCallback((vw: number, vh: number) => ({
    x: vw - FAB - 24,
    y: vh - FAB - (window.innerWidth <= MOBILE_MAX ? BOTTOM_NAV + 20 : 24),
  }), []);
  const fabDrag = useDraggable({
    storageKey: 'atlas.chitti.bubble', initial: fabInitial, size: fabSize, margin: 12,
  });


  return (
    <>
      {/* The bubble is the launcher, so it steps aside while the panel it opened is up. */}
      {!open && (
        <Tooltip title={unread ? `Chitti — ${unread} new answer` : 'Ask Chitti'} placement="left">
          <Box
            component="button"
            type="button"
            {...fabDrag.handleProps}
            // A press that travelled is a move, not a click — otherwise parking the
            // bubble would also open it.
            onClick={() => { if (fabDrag.wasTap()) setOpen(true); }}
            aria-label={unread
              ? `Ask Chitti — ${unread} answer(s) you have not read`
              : 'Ask Chitti'}
            aria-expanded={open}
            sx={{
              position: 'fixed', left: fabDrag.pos.x, top: fabDrag.pos.y,
              width: FAB, height: FAB, borderRadius: '50%', p: 0,
              zIndex: 1200,
              display: 'flex', alignItems: 'center', justifyContent: 'center',
              cursor: fabDrag.dragging ? 'grabbing' : 'grab',
              border: '1px solid rgba(255,255,255,.18)',
              background: `linear-gradient(135deg, ${tokens.teal} 0%, ${tokens.tealHi} 100%)`,
              color: '#fff',
              boxShadow: '0 8px 22px rgba(13,115,119,.45)',
              transition: 'box-shadow 160ms, transform 160ms',
              '&:hover': { boxShadow: '0 10px 28px rgba(13,115,119,.6)' },
              '&:focus-visible': { outline: `3px solid ${tokens.navy2}`, outlineOffset: 3 },
              '@media print': { display: 'none' },
            }}
          >
            <Badge
              badgeContent={unread}
              max={9}
              overlap="circular"
              sx={{ '& .MuiBadge-badge': {
                bgcolor: '#F0B43C', color: '#2A1B00', fontWeight: 800, fontSize: 10,
                pointerEvents: 'none' } }}
            >
              {/* Decorative: the real control is the parent, and anything that reads as a
                  control here makes the drag guard yield and freezes the bubble. */}
              <ChatBubbleOutlineIcon aria-hidden sx={{ fontSize: 26, display: 'block' }} />
            </Badge>
          </Box>
        </Tooltip>
      )}
      {open && <ChittiPanel mobile={mobile} />}
    </>
  );
}

function ChittiPanel({ mobile }: { mobile: boolean }) {
  const {
    messages, input, setInput, busy, error, memoryAvailable, send, cancel, reset, setOpen, ready,
    editing, setEditing,
  } = useChitti();
  const [rolled, setRolled] = useState(false);
  // Where the panel was parked before it was minimised, so expanding puts it back
  // instead of quietly stranding it in the corner it was docked to.
  const parked = useRef<Point | null>(null);
  const paperRef = useRef<HTMLDivElement | null>(null);
  const endRef = useRef<HTMLDivElement | null>(null);
  const fieldRef = useRef<HTMLInputElement | null>(null);

  const size = useCallback(() => ({ w: PANEL_W, h: rolled ? HEADER_H : PANEL_H }), [rolled]);
  const initial = useCallback((vw: number, vh: number) => ({
    x: vw - PANEL_W - 24, y: Math.max(16, vh - PANEL_H - 24),
  }), []);
  const drag = useDraggable({
    storageKey: 'atlas.chitti.panel', initial, size, margin: 8, enabled: !mobile,
  });

  useEffect(() => { endRef.current?.scrollIntoView({ block: 'nearest' }); }, [messages, busy]);
  useEffect(() => { if (!rolled) fieldRef.current?.focus(); }, [rolled]);

  // Dragging the minimised bar by hand is a later, more deliberate choice than the spot
  // it was auto-docked from — so it beats the remembered one, and expanding stays put.
  useEffect(() => { if (drag.dragging && rolled) parked.current = null; }, [drag.dragging, rolled]);

  // Escape closes — but only while focus is inside the panel, because the page behind is
  // live and Escape belongs to whatever the user is actually working in.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if (paperRef.current?.contains(document.activeElement)) { e.stopPropagation(); setOpen(false); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [setOpen]);

  const placement = useMemo(() => (mobile
    ? { left: 0, right: 0, top: 'auto' as const,
        bottom: `calc(${BOTTOM_NAV}px + env(safe-area-inset-bottom))`,
        width: '100%', height: rolled ? HEADER_H : '70vh', borderRadius: '14px 14px 0 0' }
    : { left: drag.pos.x, top: drag.pos.y, width: PANEL_W,
        height: rolled ? HEADER_H : PANEL_H, borderRadius: '12px' }
  ), [mobile, rolled, drag.pos.x, drag.pos.y]);

  // Minimising sends the collapsed title bar back to the bottom-right corner — the place
  // the user expects to find Chitti when it is out of the way, wherever they had dragged
  // the open panel to. Expanding returns it to that spot, so a minimise round trip does
  // not silently undo their drag. On a phone the sheet is already docked, so it just rolls.
  const CORNER = 24;
  const toggleRolled = () => {
    if (mobile) { setRolled(r => !r); return; }
    if (!rolled) {
      parked.current = drag.pos;
      setRolled(true);
      drag.moveTo(
        { x: window.innerWidth - PANEL_W - CORNER, y: window.innerHeight - HEADER_H - CORNER },
        { w: PANEL_W, h: HEADER_H });     // it is about to BE header-high, not PANEL_H
    } else {
      setRolled(false);
      if (parked.current) drag.moveTo(parked.current, { w: PANEL_W, h: PANEL_H });
      parked.current = null;
    }
  };

  // Editing re-asks an earlier question in place, dropping its old answer and everything
  // after it — so the composer is also the edit box, with the draft held in `editing`.
  const draft = editing ? editing.text : input;
  const setDraft = (v: string) => (editing ? setEditing({ ...editing, text: v }) : setInput(v));
  const submit = () => {
    if (!draft.trim() || busy) return;
    void (editing ? send(editing.text, editing.index) : send());
  };

  return (
    <Paper
      ref={paperRef}
      elevation={12}
      role="region"
      aria-label="Chitti assistant"
      className="no-print"
      sx={{
        position: 'fixed', ...placement,
        // Above the app chrome, below MUI's modals (1300) and below VocX's panel, so a
        // live capture still wins the top of the stack.
        zIndex: 1240,
        display: 'flex', flexDirection: 'column', overflow: 'hidden',
        border: `1px solid ${tokens.line}`,
        boxShadow: '0 18px 48px rgba(16,32,48,.28)',
        '@media print': { display: 'none' },
      }}
    >
      {/* Header — the drag handle on a desktop, a plain title bar on a phone. */}
      <Box
        {...(mobile ? {} : drag.handleProps)}
        sx={{
          display: 'flex', alignItems: 'center', gap: 0.5, px: 1, flexShrink: 0,
          height: HEADER_H,
          background: `linear-gradient(135deg, ${tokens.navy} 0%, ${tokens.navy2} 100%)`,
          color: '#fff',
          cursor: mobile ? 'default' : (drag.dragging ? 'grabbing' : 'grab'),
          userSelect: 'none',
        }}
      >
        {!mobile && <DragIndicatorIcon sx={{ fontSize: 18, color: 'rgba(255,255,255,.55)' }} />}
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <Typography sx={{ fontSize: 13.5, fontWeight: 700, lineHeight: 1.1 }}>Chitti</Typography>
          <Typography sx={{ fontSize: 9.5, letterSpacing: '.14em', color: 'rgba(255,255,255,.62)',
            whiteSpace: 'nowrap' }}>
            EVAM · ASK YOUR BOOK
          </Typography>
        </Box>
        <Tooltip title="New conversation">
          <span>
            <IconButton size="small" onClick={reset} disabled={!messages.length && !input}
              aria-label="Start a new conversation"
              sx={{ color: 'rgba(255,255,255,.75)', '&:hover': { color: '#fff' },
                '&.Mui-disabled': { color: 'rgba(255,255,255,.25)' } }}>
              <RestartAltIcon sx={{ fontSize: 17 }} />
            </IconButton>
          </span>
        </Tooltip>
        <Tooltip title={rolled ? 'Expand' : 'Minimise to the corner'}>
          <IconButton size="small" onClick={toggleRolled}
            aria-label={rolled ? 'Expand Chitti' : 'Minimise Chitti'}
            sx={{ color: 'rgba(255,255,255,.75)', '&:hover': { color: '#fff' } }}>
            {rolled ? <OpenInFullIcon sx={{ fontSize: 15 }} /> : <MinimizeIcon sx={{ fontSize: 16 }} />}
          </IconButton>
        </Tooltip>
        <Tooltip title="Close">
          <IconButton size="small" onClick={() => setOpen(false)} aria-label="Close Chitti"
            sx={{ color: 'rgba(255,255,255,.75)', '&:hover': { color: '#fff' } }}>
            <CloseIcon sx={{ fontSize: 17 }} />
          </IconButton>
        </Tooltip>
      </Box>

      {/* Hidden rather than unmounted while rolled up, so a streaming answer keeps going
          and the scroll position is still there when it is opened again. */}
      <Box sx={{ display: rolled ? 'none' : 'flex', flexDirection: 'column',
                 flex: 1, minHeight: 0, minWidth: 0 }}>
        <Box
          role="log"
          aria-label="Conversation"
          aria-live="polite"
          aria-relevant="additions text"
          sx={{ flex: 1, minHeight: 0, overflowY: 'auto', overflowX: 'hidden',
                px: 1.25, py: 1.25, bgcolor: tokens.paper }}
        >
          {!ready && <Alert severity="info" sx={{ fontSize: 12 }}>
            Sign in with your work account to use Chitti.</Alert>}
          {ready && !messages.length && (
            <Box sx={{ color: tokens.muted, textAlign: 'center', mt: 4, px: 2 }}>
              <ChatBubbleOutlineIcon sx={{ fontSize: 30, opacity: 0.35 }} />
              <Typography sx={{ fontSize: 13, fontWeight: 600, mt: 1, color: tokens.ink }}>
                What would you like to know?
              </Typography>
              <Typography sx={{ fontSize: 11.5, mt: 0.5 }}>
                Ask about your book, clients and deals.
              </Typography>
            </Box>
          )}
          {messages.map((message, index) => {
            const mine = message.role === 'user';
            const waiting = busy && index === messages.length - 1 && !message.content;
            return (
              <Box key={index} sx={{ display: 'flex', mb: 1,
                                     justifyContent: mine ? 'flex-end' : 'flex-start' }}>
                {/* Left of the right-aligned bubble, so it never crowds the panel edge. */}
                {mine && !busy && (
                  <Tooltip title="Edit and ask again">
                    <IconButton
                      size="small"
                      onClick={() => setEditing({ index, text: message.content })}
                      aria-label={`Edit question ${messages.slice(0, index + 1).filter(m => m.role === 'user').length}`}
                      sx={{ alignSelf: 'center', mr: 0.25, color: 'rgba(95,110,118,.4)',
                            '&:hover': { color: tokens.muted } }}
                    >
                      <EditIcon sx={{ fontSize: 13 }} />
                    </IconButton>
                  </Tooltip>
                )}
                <Box sx={{
                  maxWidth: mine ? '85%' : '100%', minWidth: 0,
                  px: 1.25, py: 0.9, borderRadius: '10px',
                  bgcolor: mine ? tokens.teal : tokens.card,
                  color: mine ? '#fff' : tokens.ink,
                  border: mine ? 'none' : `1px solid ${tokens.line}`,
                }}>
                  {waiting ? (
                    <Stack direction="row" spacing={1} alignItems="center" role="status"
                      aria-live="polite" aria-atomic="true">
                      <CircularProgress size={13} aria-hidden="true" />
                      <Typography sx={{ fontSize: 12, color: tokens.muted, minWidth: 0,
                        whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>
                        {message.progress || 'Working on your question…'}
                      </Typography>
                    </Stack>
                  ) : (
                    // Model prose stays escaped — React's default — exactly as on the page.
                    <Typography sx={{ fontSize: 12.5, whiteSpace: 'pre-wrap',
                      overflowWrap: 'anywhere' }}>
                      {message.content || 'Answer stopped.'}
                    </Typography>
                  )}
                  {message.evidence?.map(e => (
                    <Typography key={e.reference} sx={{ fontSize: 11, mt: 0.5,
                      color: mine ? 'rgba(255,255,255,.75)' : tokens.muted }}>
                      [{e.reference}] {e.label}
                    </Typography>
                  ))}
                  {/* Read down, not across — a grid this wide only fits the page. */}
                  {message.tables?.map((table, i) => (
                    <RecordList key={i} result={table} />
                  ))}
                  {message.role === 'assistant' && message.content && !message.complete && !busy && (
                    <Typography sx={{ fontSize: 10.5, color: tokens.muted }}>Incomplete answer</Typography>
                  )}
                  {/* The one thing support asks for when an answer goes wrong. */}
                  {message.requestId && (
                    <Typography sx={{ fontSize: 10, color: tokens.muted, mt: 0.5 }}>
                      Request ID: {message.requestId}
                    </Typography>
                  )}
                </Box>
              </Box>
            );
          })}
          <div ref={endRef} />
        </Box>

        {error && <Alert severity="error" sx={{ borderRadius: 0, fontSize: 11.5, py: 0.25 }}>{error}</Alert>}
        {!memoryAvailable && (
          <Alert severity="warning" sx={{ borderRadius: 0, fontSize: 11.5, py: 0.25 }}>
            This tab cannot save the conversation.
          </Alert>
        )}

        {editing && (
          <Stack direction="row" alignItems="center" spacing={1}
            sx={{ px: 1, py: 0.5, bgcolor: '#FFF6E5', borderTop: `1px solid ${tokens.line}`,
                  flexShrink: 0 }}>
            <Typography sx={{ fontSize: 11, color: tokens.ink, flex: 1, minWidth: 0 }}>
              Editing — sending drops the old answer and everything after it.
            </Typography>
            <Typography component="button" type="button" onClick={() => setEditing(null)}
              sx={{ fontSize: 11, fontWeight: 600, color: tokens.teal, background: 'none',
                    border: 0, cursor: 'pointer', p: 0 }}>
              Cancel
            </Typography>
          </Stack>
        )}
        <Box
          component="form"
          onSubmit={e => { e.preventDefault(); submit(); }}
          sx={{ display: 'flex', alignItems: 'flex-end', gap: 0.75, p: 1,
                borderTop: `1px solid ${tokens.line}`, bgcolor: tokens.card, flexShrink: 0 }}
        >
          <TextField
            fullWidth multiline minRows={1} maxRows={4} inputRef={fieldRef}
            placeholder="Ask Chitti…"
            value={draft} disabled={!ready}
            onChange={e => setDraft(e.target.value)}
            // aria-label has to ride on inputProps: passed to TextField directly it lands
            // on the wrapping FormControl div and the textarea is left unnamed.
            inputProps={{ maxLength: 8000,
              'aria-label': editing ? 'Edit your question' : 'Your question' }}
            onKeyDown={e => {
              // Escape backs out of the EDIT first and stops there — letting it reach the
              // panel's own Escape handler would shut Chitti entirely, which is a long way
              // from "never mind, keep the question as it was".
              if (e.key === 'Escape' && editing) {
                e.preventDefault(); e.stopPropagation(); setEditing(null); return;
              }
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault(); submit();
              }
            }}
          />
          {busy ? (
            <Tooltip title="Stop answer">
              <IconButton size="small" onClick={cancel} aria-label="Stop answer"
                sx={{ color: tokens.bad }}>
                <StopCircleIcon />
              </IconButton>
            </Tooltip>
          ) : (
            <Tooltip title="Send · Enter">
              <span>
                <IconButton size="small" type="submit" aria-label="Send question"
                  disabled={!ready || !draft.trim()}
                  sx={{ color: tokens.teal, '&.Mui-disabled': { color: tokens.line } }}>
                  <SendIcon />
                </IconButton>
              </span>
            </Tooltip>
          )}
        </Box>
      </Box>
    </Paper>
  );
}
