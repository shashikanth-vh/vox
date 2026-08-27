import { useRef, useState, useEffect } from "react";
import { Box, Button, GlobalStyles } from "@mui/material";
import { Outlet, useNavigate, useLocation } from "react-router-dom";
import Navbar from "./Navbar";
import BottomNav from "./BottomNav";
import Copilot from "../copilot/Copilot";
import { VocxProvider, useVocx } from "../vocx/VocxProvider";
import VocxPanel from "../vocx/VocxPanel";
import { NAV } from "./navConfig";
import { useAuth } from "../../auth/AuthContext";
import { canSee } from "../../auth/rbac";
import { tokens } from "../../theme";

const MOBILE = "@media (max-width:760px)";

// Edge control for the tab strip. Rendered (dimmed) even at the end of its travel so
// the bar always reads as movable rather than the affordance vanishing.
function NavArrow({
  dir,
  show,
  disabled,
  onClick,
  badge = 0,
}: {
  dir: "left" | "right";
  show: boolean;
  disabled: boolean;
  onClick: () => void;
  badge?: number;
}) {
  if (!show) return null;
  return (
    <Box
      onClick={disabled ? undefined : onClick}
      title={disabled ? "" : dir === "right" ? "More tabs" : "Previous tabs"}
      sx={{
        display: "flex",
        alignItems: "center",
        gap: "2px",
        px: "8px",
        flexShrink: 0,
        color: disabled ? "rgba(175,194,201,.35)" : "#AFC2C9",
        cursor: disabled ? "default" : "pointer",
        userSelect: "none",
        fontSize: 15,
        fontWeight: 700,
        // A soft inner shadow hints that content continues past the edge.
        boxShadow: disabled
          ? "none"
          : dir === "right"
            ? "inset 10px 0 12px -10px rgba(0,0,0,.55)"
            : "inset -10px 0 12px -10px rgba(0,0,0,.55)",
        "&:hover": {
          color: disabled ? undefined : "#fff",
          bgcolor: disabled ? undefined : "rgba(255,255,255,.08)",
        },
      }}
    >
      {dir === "left" ? "‹" : "›"}
      {badge > 0 && (
        <Box
          component="span"
          sx={{
            fontSize: 10,
            fontWeight: 800,
            lineHeight: 1,
            px: "5px",
            py: "2px",
            borderRadius: "99px",
            bgcolor: "rgba(255,255,255,.14)",
            color: "#fff",
          }}
        >
          +{badge}
        </Box>
      )}
    </Box>
  );
}

export default function AppLayout() {
  const { user } = useAuth();
  const nav = useNavigate();
  const loc = useLocation();
  const visible = NAV.filter((n) => canSee(user.roles, n.tab));

  // The strip shows a window of PAGE tabs; the rest wait off-stage. Clicking the
  // last tab in the window slides it forward by STEP to reveal what's next
  // (clicking the first slides it back), so every tab is reachable in one hop.
  const PAGE = 11,
    STEP = 2;
  const [start, setStart] = useState(0);
  const maxStart = Math.max(0, visible.length - PAGE);
  const from = Math.min(start, maxStart);
  const shown = visible.slice(from, from + PAGE);

  // A deep link (or a role change) can land on a tab outside the window — bring it in.
  useEffect(() => {
    const i = visible.findIndex((n) => n.path === loc.pathname);
    if (i < 0) return;
    setStart((s) => {
      const cur = Math.min(s, maxStart);
      if (i < cur) return i;
      if (i > cur + PAGE - 1) return Math.min(maxStart, i - PAGE + 1);
      return cur;
    });
  }, [loc.pathname, visible.length, maxStart]);

  const onTabClick = (path: string, i: number) => {
    nav(path);
    if (i === PAGE - 1 && from < maxStart)
      setStart(Math.min(maxStart, from + STEP));
    else if (i === 0 && from > 0) setStart(Math.max(0, from - STEP));
  };

  // The tab strip scrolls but its scrollbar is hidden, so give it grab-and-drag
  // (and shift-free wheel) scrolling — otherwise overflowed tabs are unreachable
  // on a desktop with no horizontal wheel.
  const strip = useRef<HTMLDivElement>(null);
  const drag = useRef({ down: false, startX: 0, startScroll: 0, moved: 0 });

  const onPointerDown = (e: React.PointerEvent) => {
    const el = strip.current;
    if (!el) return;
    drag.current = {
      down: true,
      startX: e.clientX,
      startScroll: el.scrollLeft,
      moved: 0,
    };
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const el = strip.current;
    const d = drag.current;
    if (!el || !d.down) return;
    const dx = e.clientX - d.startX;
    d.moved = Math.max(d.moved, Math.abs(dx));
    if (d.moved > 4) {
      // Only capture once we're clearly dragging, so a plain tap still clicks through.
      if (e.currentTarget.hasPointerCapture?.(e.pointerId) === false)
        e.currentTarget.setPointerCapture(e.pointerId);
      el.scrollLeft = d.startScroll - dx;
    }
  };
  const endDrag = (e: React.PointerEvent) => {
    drag.current.down = false;
    if (e.currentTarget.hasPointerCapture?.(e.pointerId))
      e.currentTarget.releasePointerCapture(e.pointerId);
  };
  // A drag that moved past the threshold must not also activate the tab under the cursor.
  const onClickCapture = (e: React.MouseEvent) => {
    if (drag.current.moved > 4) {
      e.preventDefault();
      e.stopPropagation();
      drag.current.moved = 0;
    }
  };
  const onWheel = (e: React.WheelEvent) => {
    const el = strip.current;
    if (!el) return;
    const d = Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY;
    if (d) el.scrollLeft += d;
  };

  return (
   <VocxProvider>
    <Box
      sx={{
        height: "100dvh",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        bgcolor: tokens.paper,
        // When printing (PDF export), let the frame expand so the whole view prints.
        "@media print": { height: "auto", overflow: "visible" },
      }}
    >
      <GlobalStyles
        styles={{
          // Lock the page itself — only the main content area scrolls, so no window
          // scrollbar appears outside the app.
          html: { WebkitTapHighlightColor: "transparent", height: "100%", overflow: "hidden" },
          body: { overscrollBehaviorY: "none", height: "100%", overflow: "hidden", margin: 0 },
          "#root": { height: "100%", overflow: "hidden" },
          // iOS zooms into any focused field under 16px — keep them at 16px on phones
          [MOBILE]: {
            "input, select, textarea": { fontSize: "16px !important" },
          },
          // PDF export = window.print(). Hide the app chrome + export controls and let
          // the content expand so the saved PDF is a clean render of just the view.
          "@media print": {
            ".no-print": { display: "none !important" },
            "html, body, #root": { height: "auto !important", overflow: "visible !important" },
          },
        }}
      />
      <Navbar />
      {/* Tab bar: chevrons make the "there's more this way" affordance visible instead
          of leaving it to be discovered by dragging. */}
      <Box
        className="no-print"
        sx={{
          display: "flex",
          alignItems: "stretch",
          flexShrink: 0,
          zIndex: 55,
          background: `linear-gradient(180deg, ${tokens.navy2}, #1E2F4F)`,
          boxShadow: "inset 0 -1px 0 rgba(255,255,255,.05)",
          [MOBILE]: { display: "none" },
        }}
      >
        <NavArrow
          dir="left"
          show={maxStart > 0}
          disabled={from === 0}
          onClick={() => setStart(Math.max(0, from - STEP))}
        />
        <Box
          component="nav"
          ref={strip}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={endDrag}
          onPointerCancel={endDrag}
          onClickCapture={onClickCapture}
          onWheel={onWheel}
          sx={{
            // v17 nav: 8px/14px padding, 6px gap.
            display: "flex",
            gap: "6px",
            px: "14px",
            py: "8px",
            flex: 1,
            minWidth: 0,
            overflowX: "auto",
            cursor: "grab",
            "&:active": { cursor: "grabbing" },
            // Let the browser own vertical gestures; we drive the horizontal axis.
            touchAction: "pan-y",
            userSelect: "none",
            "&::-webkit-scrollbar": { display: "none" },
            scrollbarWidth: "none",
          }}
        >
          {shown.map((n, i) => {
            const active = loc.pathname === n.path;
            return (
              <Button
                key={n.path}
                onClick={() => onTabClick(n.path, i)}
                disableRipple
                sx={{
                  color: active ? "#fff" : "#AFC2C9",
                  px: "15px",
                  py: "8px",
                  fontSize: 12.8,
                  fontWeight: active ? 700 : 600,
                  lineHeight: 1,
                  whiteSpace: "nowrap",
                  borderRadius: "99px",
                  border: "1px solid transparent",
                  background: active
                    ? `linear-gradient(135deg, ${tokens.teal} 0%, ${tokens.tealHi} 100%)`
                    : "transparent",
                  borderColor: active ? "rgba(255,255,255,.16)" : "transparent",
                  boxShadow: active
                    ? "0 4px 14px rgba(13,115,119,.45), inset 0 1px 0 rgba(255,255,255,.22)"
                    : "none",
                  "&:hover": {
                    color: "#fff",
                    background: active
                      ? `linear-gradient(135deg, ${tokens.teal} 0%, ${tokens.tealHi} 100%)`
                      : "rgba(255,255,255,.08)",
                  },
                  "&:hover .tabic": { filter: "grayscale(0)", opacity: 1 },
                  "& .tabic": {
                    fontSize: 12.5,
                    mr: "6px",
                    display: "inline-block",
                    verticalAlign: "-1px",
                    filter: active ? "grayscale(0)" : "grayscale(.45)",
                    opacity: active ? 1 : 0.85,
                    transition: "filter .15s ease, opacity .15s ease",
                  },
                }}
              >
                <Box component="span" className="tabic">
                  {n.icon}
                </Box>
                {n.label}
              </Button>
            );
          })}
        </Box>
        <NavArrow
          dir="right"
          show={maxStart > 0}
          disabled={from >= maxStart}
          onClick={() => setStart(Math.min(maxStart, from + STEP))}
          badge={
            maxStart > 0 && from < maxStart ? visible.length - (from + PAGE) : 0
          }
        />
      </Box>
      <Box
        component="main"
        sx={{ flex: 1, minHeight: 0, overflowY: "auto", overflowX: "hidden", "@media print": { overflow: "visible" } }}
      >
        <Box
          sx={{
            maxWidth: 1500,
            mx: "auto",
            px: 2.2,
            py: 2,
            pb: 9,
            [MOBILE]: {
              px: 1.25,
              py: 1.5,
              // 76px cleared the BOTTOM NAV but not the copilot bubble floating
              // above it — a list's last row (its action button especially) sat
              // under the bubble with no way to scroll past. Clear both.
              pb: "calc(150px + env(safe-area-inset-bottom))",
            },
          }}
        >
          <Outlet />
        </Box>
      </Box>
      <BottomNav />
      {/* v17 AUGMENT 12 — floating ATLAS Copilot (chat over the live Register). */}
      <Copilot />
      {/* VocX's floating panel. Mounted at the layout root, not inside a page, so an
          in-progress capture survives navigation — an RM can start a note on Lending, go
          and check something on Deals, and come back to it. (The launcher itself lives in
          the navbar on every width.) */}
      <VocxPanelMount />
    </Box>
   </VocxProvider>
  );
}

/** Reads the shared open state so the panel need not be threaded through the layout. */
function VocxPanelMount() {
  const { open, setOpen } = useVocx();
  return <VocxPanel open={open} onClose={() => setOpen(false)} />;
}
