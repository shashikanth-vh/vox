import { useMemo, useState, useEffect, useRef, Fragment, createContext, useContext } from "react";
import {
  MaterialReactTable,
  useMaterialReactTable,
  MRT_ToggleGlobalFilterButton,
  MRT_ShowHideColumnsButton,
  MRT_ToggleFullScreenButton,
  type MRT_ColumnDef,
  type MRT_ColumnFiltersState,
  type MRT_SortingState,
  type MRT_PaginationState,
} from "material-react-table";
import {
  Box, IconButton, Tooltip, Stack, Popover, Paper, Typography, InputBase, Drawer,
  MenuItem, ListItemIcon, Divider, Badge, Button, useMediaQuery,
} from "@mui/material";
import FilterAltIcon from "@mui/icons-material/FilterAlt";
import SearchIcon from "@mui/icons-material/Search";
import TuneIcon from "@mui/icons-material/Tune";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import ChevronLeftIcon from "@mui/icons-material/ChevronLeft";
import ChevronRightIcon from "@mui/icons-material/ChevronRight";
import VisibilityIcon from "@mui/icons-material/Visibility";
import EditIcon from "@mui/icons-material/Edit";
import DeleteIcon from "@mui/icons-material/Delete";
import BlockSharpIcon from "@mui/icons-material/BlockSharp";
import DownloadIcon from "@mui/icons-material/Download";
import PictureAsPdfIcon from "@mui/icons-material/PictureAsPdf";
import FileDownloadIcon from "@mui/icons-material/FileDownload";
import { useQuery } from "@tanstack/react-query";
import { useSearch } from "../../context/SearchContext";
import { tokens } from "../../theme";
import { USE_REAL_API } from "../../api/http";
import type { Paged, TableQuery } from "../../services/types";

// v12 `th` background (ATLAS_EVAM_v12.html line 97).
const V12_HEAD_BG = "#F7F9FA";

// Same phone breakpoint the navbar and bottom nav use.
const MOBILE_QUERY = "(max-width:760px)";

/**
 * The headline of a phone card — what the row is, and its one number worth reading at a
 * glance:
 *
 *   primary ................ value
 *
 * Underneath, the card lists EVERY column the grid has, so nothing is lost by being on a
 * phone. These two slots only decide what gets promoted above that list.
 *
 * They take a render function rather than a column id so a page hands over the SAME node
 * the grid cell renders — e.g. the clickable name that filters the Dashboard keeps its
 * click.
 */
export interface MobileCardSpec<T> {
  primary: (row: T) => React.ReactNode;
  value?: (row: T) => React.ReactNode;
}

// A committed column filter is just the SET OF CHECKED VALUES. The popup's text box
// only narrows which options are shown — it does not filter the table itself; ticking
// values only STAGES them, and the table filters when you click Done. Filters live in
// CommonTable state (not MRT's).
interface FilterCtxT {
  get: (id: string) => string[] | undefined;
  set: (id: string, v: string[] | undefined) => void;
}
const FilterContext = createContext<FilterCtxT>({
  get: () => undefined,
  set: () => {},
});

// v12 `.secondary` / `.primary` buttons.
const SEC_BTN: React.CSSProperties = {
  background: "#fff",
  border: "1px solid #E2E7EA",
  borderRadius: 8,
  padding: "5px 12px",
  fontSize: 12.2,
  fontWeight: 600,
  cursor: "pointer",
  color: "#17222B",
};
const PRI_BTN: React.CSSProperties = {
  background: "#0D7377",
  border: "none",
  borderRadius: 8,
  padding: "5px 12px",
  fontSize: 12.2,
  fontWeight: 600,
  cursor: "pointer",
  color: "#fff",
};

// The popover body. The search box filters the OPTIONS shown (local only). Ticking a
// value only STAGES it in local state; the selection is committed (and the table
// filtered) when the user clicks Done. Closing without Done discards the staged edits.
function FilterBody({
  value,
  options,
  onChange,
  onClose,
}: {
  value: string[] | undefined;
  options: string[];
  onChange: (v: string[] | undefined) => void;
  onClose: () => void;
}) {
  const [search, setSearch] = useState("");
  const [vals, setVals] = useState<string[]>(value ?? []);
  const commit = (nv: string[]) => onChange(nv.length ? nv : undefined);
  // Checkbox clicks only stage the selection locally — no commit here.
  const onToggle = (v: string, on: boolean) => {
    setVals((prev) => (on ? [...prev, v] : prev.filter((x) => x !== v)));
  };
  const shown = search.trim()
    ? options.filter((o) =>
        o.toLowerCase().includes(search.trim().toLowerCase()),
      )
    : options;

  return (
    // stopPropagation: portal clicks bubble through the React tree, so without this
    // a checkbox click would also reach the header's sort handler.
    <Box
      sx={{ width: 230, fontSize: "12.3px", p: "11px" }}
      onClick={(e) => e.stopPropagation()}
    >
      <Box
        component="input"
        type="text"
        placeholder="Search values…"
        value={search}
        autoFocus
        onChange={(e: any) => setSearch(e.target.value)}
        sx={{
          width: "100%",
          border: "1px solid #E2E7EA",
          borderRadius: "6px",
          p: "6px 8px",
          mb: "7px",
          fontSize: "12.3px",
          boxSizing: "border-box",
          "&:focus": { outline: "none", borderColor: "#12917E" },
        }}
      />
      <Box
        sx={{
          maxHeight: 190,
          overflow: "auto",
          borderTop: "1px solid #F0F3F4",
          pt: "6px",
        }}
      >
        {shown.map((v) => (
          <Box
            component="label"
            key={v}
            sx={{
              display: "flex",
              gap: "7px",
              alignItems: "center",
              p: "3px 1px",
              cursor: "pointer",
            }}
          >
            <input
              type="checkbox"
              checked={vals.includes(v)}
              style={{ accentColor: "#0D7377" }}
              onChange={(e) => onToggle(v, e.target.checked)}
            />
            {v === "" ? <i>(blank)</i> : v}
          </Box>
        ))}
        {shown.length === 0 && (
          <Box sx={{ color: "#5F6E76", p: "4px 1px" }}>
            {options.length ? "No matching values" : "No values"}
          </Box>
        )}
      </Box>
      <Box
        sx={{
          display: "flex",
          gap: "7px",
          justifyContent: "flex-end",
          mt: "9px",
        }}
      >
        <button
          style={SEC_BTN}
          onClick={() => {
            setVals([]);
            setSearch("");
            onChange(undefined);
            onClose();
          }}
        >
          Clear
        </button>
        <button
          style={PRI_BTN}
          onClick={() => {
            commit(vals);
            onClose();
          }}
        >
          Done
        </button>
      </Box>
    </Box>
  );
}

// v12 header: the label plus a funnel that opens the filter popover. Sorting is
// left to MRT's native sort icon (we only add the funnel here). The funnel turns
// teal when the column is filtered.
function FilterHeader({
  colId,
  label,
  options,
  canFilter,
}: {
  colId: string;
  label: string;
  options: string[];
  canFilter: boolean;
}) {
  const ctx = useContext(FilterContext);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const value = ctx.get(colId);
  const active = !!(value && value.length);

  return (
    <Box
      sx={{ display: "flex", alignItems: "center", gap: "4px", width: "100%", minWidth: 0 }}
    >
      {/* Label takes the leftover width and truncates with an ellipsis so the funnel
          (fixed 20px, never shrinks) is always fully visible. */}
      <Box sx={{ flex: 1, minWidth: 0, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{label}</Box>
      {canFilter && (
        <>
          {/* stopPropagation so clicking the funnel opens the filter, not sorts.
              A fixed 20px box (18px glyph + 1px pad each side) that never shrinks; the
              gap to the sort arrow is owned by the Content-Labels row gap, not a margin. */}
          <IconButton
            size="small"
            onClick={(e) => {
              e.stopPropagation();
              setAnchor(e.currentTarget);
            }}
            sx={{
              p: "1px",
              flex: "0 0 auto",
              width: 20,
              height: 20,
              color: active ? "#0D7377" : "#8493A0",
            }}
          >
            <FilterAltIcon sx={{ fontSize: 18 }} />
          </IconButton>
          <Popover
            open={!!anchor}
            anchorEl={anchor}
            onClose={() => setAnchor(null)}
            anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
            transformOrigin={{ vertical: "top", horizontal: "right" }}
            slotProps={{
              paper: {
                sx: {
                  border: "1px solid #E2E7EA",
                  borderRadius: "10px",
                  boxShadow: "0 14px 38px rgba(10,20,35,.25)",
                  mt: 0.5,
                },
              },
            }}
          >
            <FilterBody
              value={value}
              options={options}
              onClose={() => setAnchor(null)}
              onChange={(v) => ctx.set(colId, v)}
            />
          </Popover>
        </>
      )}
    </Box>
  );
}

export interface RowAction<T> {
  onView?: (row: T) => void;
  onEdit?: (row: T) => void;
  onDelete?: (row: T) => void;
  /** Set when the row IS editable in principle but not by this user — the reason shown. */
  editReason?: string;
}

interface CommonTableProps<T extends Record<string, any>> extends RowAction<T> {
  queryKey: unknown[];
  fetcher: (q: TableQuery) => Promise<Paged<T>>;
  columns: MRT_ColumnDef<T>[];
  searchFields?: string[];
  csvName?: string;
  /** page-level toolbar content rendered on the left (e.g. Add button) */
  toolbarLeft?: React.ReactNode;
  enableExpand?: boolean; // requirement 11: toggle table expand
  actionsEnabled?: boolean; // hide the sticky action column when false
  onRowClick?: (row: T) => void; // click a row body (not an action/link) to act on it
  rowSx?: (row: T) => object; // per-row cell styling (v12 gray/rowok/rowbad states)
  extraActions?: (row: T) => React.ReactNode; // extra icon(s) in the action column
  /**
   * What heads this table's card on a phone. Below 760px the grid is replaced by a list
   * of cards, each carrying every column; this only names the one or two fields promoted
   * to the card's title line. Desktop ignores it, and omitting it just falls back to the
   * first column.
   */
  mobileCard?: MobileCardSpec<T>;
}

export default function CommonTable<T extends Record<string, any>>(
  props: CommonTableProps<T>,
) {
  const {
    queryKey,
    fetcher,
    columns,
    searchFields,
    csvName = "export",
    toolbarLeft,
    onRowClick,
    enableExpand = false,
    actionsEnabled = true,
    onView,
    onEdit,
    editReason,
    onDelete,
    rowSx,
    extraActions,
    mobileCard,
  } = props;

  // Phone gets the card list; every width above 760px is the grid, untouched.
  const isMobile = useMediaQuery(MOBILE_QUERY);

  // Size the actions column to how many icons this table actually renders (row-CSV is
  // always there; +View/+Edit/+Delete/+extra when supplied) so it stays snug rather than
  // reserving a wide fixed block. ~24px per 16px icon gives proper spacing; the floor
  // keeps the "Actions" header from clipping.
  const actionCount = 1 + (onView ? 1 : 0) + (onEdit ? 1 : 0) + (onDelete ? 1 : 0) + (extraActions ? 1 : 0);
  const actionsColSize = Math.max(64, actionCount * 24 + 10);

  const { search } = useSearch(); // requirement 15: navbar search drives the table
  const [pagination, setPagination] = useState<MRT_PaginationState>(() => ({
    pageIndex: 0,
    // The card list pulls a bigger page: the same server call, but fewer round trips to
    // fill a screen the user is flicking through.
    pageSize: isMobile ? 20 : 10,
  }));
  const [sorting, setSorting] = useState<MRT_SortingState>([]);
  const [globalFilter, setGlobalFilter] = useState("");

  // Our own column-filter state, keyed by column id (bypassing MRT's filter model).
  // Each value is the set of checked options for that column.
  const [colFilters, setColFilters] = useState<Record<string, string[]>>({});
  const filterCtx = useMemo<FilterCtxT>(
    () => ({
      get: (id) => colFilters[id],
      set: (id, v) =>
        setColFilters((p) => {
          const n = { ...p };
          if (!v || !v.length) delete n[id];
          else n[id] = v;
          return n;
        }),
    }),
    [colFilters],
  );
  // Shape our filters into the TableQuery array the fetcher/query engine expects.
  const columnFilters = useMemo(
    () =>
      Object.entries(colFilters)
        .filter(([, v]) => v && v.length)
        .map(([id, value]) => ({ id, value })) as MRT_ColumnFiltersState,
    [colFilters],
  );

  // Which register query param carries this column's facet. Declared per column
  // (meta.filterParam) because the UI's field names are not the wire's ("an" is the
  // register's "analyst").
  const filterParamOf = (c: MRT_ColumnDef<T>): string | undefined =>
    (c.meta as any)?.filterParam;

  // The committed facets, translated to the register's own query params via each
  // column's meta.filterParam — what the live fetch actually sends. columnFilters keeps
  // the UI ids for the mock path.

  // The per-table search box takes precedence; otherwise the navbar search applies.
  // (MRT can hand back `undefined` when the box is cleared — coerce to a string.)
  const gf = globalFilter ?? "";
  const effectiveSearch = gf.trim() ? gf : search;

  // Cursor pagination: a cursor-paged endpoint hands back the cursor for the NEXT page
  // only, so the cursor that opens each page is remembered as the user walks forward.
  // cursors[0] is undefined — the first page is the one request that carries no cursor.
  const [cursors, setCursors] = useState<Record<number, string | undefined>>({});

  // The card list keeps every page it has walked through, keyed by page index, so
  // scrolling APPENDS instead of replacing. The fetch is unchanged — the same
  // server-side paging the desktop pager drives, just not thrown away.
  const [feedPages, setFeedPages] = useState<Record<number, T[]>>({});

  // reset to first page whenever a search term or filter changes. The cursor chain goes
  // with it: cursors are only valid for the query that produced them, and so is the
  // accumulated feed.
  useEffect(() => {
    setPagination((p) => ({ ...p, pageIndex: 0 }));
    setCursors({});
    setFeedPages({});
  }, [search, globalFilter, colFilters, sorting, pagination.pageSize]);

  const cursor = cursors[pagination.pageIndex];

  // requirement 16: pagination/sorting/filtering are all server-driven
  const query = useQuery({
    queryKey: [
      ...queryKey,
      pagination,
      sorting,
      columnFilters,
      effectiveSearch,
      cursor ?? null,
    ],
    queryFn: () =>
      fetcher({
        pageIndex: pagination.pageIndex,
        pageSize: pagination.pageSize,
        globalFilter: effectiveSearch,
        sorting,
        columnFilters,
        serverFilters,
        searchFields,
        cursor,
      }),
    placeholderData: (prev) => prev,
  });

  const rows = query.data?.rows ?? [];
  const total = query.data?.total ?? 0;

  // A source that returns nextCursor is cursor-paged; one that doesn't (the mock store)
  // keeps the ordinary jump-to-any-page controls.
  const cursorMode = query.data?.nextCursor !== undefined;
  const nextCursor = query.data?.nextCursor ?? null;

  // Remember the cursor that opens the following page, so Next can ask for it.
  useEffect(() => {
    if (!nextCursor) return;
    const at = pagination.pageIndex + 1;
    setCursors((c) => (c[at] === nextCursor ? c : { ...c, [at]: nextCursor }));
  }, [nextCursor, pagination.pageIndex]);

  // File each settled page into the feed. `placeholderData` keeps the PREVIOUS page's
  // rows on screen while the next is in flight, so filing that would duplicate it —
  // hence the isPlaceholderData guard.
  useEffect(() => {
    if (!query.data || query.isPlaceholderData) return;
    const page = query.data.rows;
    setFeedPages((p) => (p[pagination.pageIndex] === page
      ? p
      : { ...p, [pagination.pageIndex]: page }));
  }, [query.data, query.isPlaceholderData, pagination.pageIndex]);

  const feedRows = useMemo(
    () => Object.keys(feedPages)
      .map(Number)
      .sort((a, b) => a - b)
      .flatMap((k) => feedPages[k]),
    [feedPages],
  );

  // More to pull? A cursor source knows only whether ONE more page exists; a counted one
  // compares against the total. Either way an empty page ends it — without that guard a
  // total that overstates the rows would spin the observer forever.
  const hasMore = cursorMode
    ? !!nextCursor
    : feedRows.length < total && rows.length > 0;

  // Scrolling near the end asks for the next page. rootMargin fires it just BEFORE the
  // sentinel is visible, so the list tends to already be longer by the time it's reached.
  const sentinel = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!isMobile) return;
    const el = sentinel.current;
    if (!el || !hasMore || query.isFetching) return;
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        setPagination((p) => ({ ...p, pageIndex: p.pageIndex + 1 }));
      }
    }, { rootMargin: "300px" });
    io.observe(el);
    return () => io.disconnect();
  }, [isMobile, hasMore, query.isFetching]);

  // Column filters are multi-select checkboxes. The option list must cover the WHOLE
  // dataset, not just the current page, so fetch the full set once (unfiltered) and
  // derive distinct values per column. In mock mode this is just a store read.
  const facetQuery = useQuery({
    queryKey: ["facets", ...queryKey],
    // The register serves at most one capped page per request, so "everything" is a
    // WALK: follow the cursor chain (bounded) and pool the rows. A facet built from
    // one page offered a silently partial option list the moment the dataset outgrew
    // the cap — the exact dishonesty that kept Company columns facet-less.
    queryFn: async () => {
      const rows: T[] = [];
      let cursor: string | undefined;
      for (let i = 0; i < 25; i++) {
        const page = await fetcher({
          pageIndex: 0, pageSize: 100000, globalFilter: "",
          sorting: [], columnFilters: [], searchFields, cursor,
        });
        rows.push(...page.rows);
        if (!page.nextCursor) break;
        cursor = page.nextCursor;
      }
      return { rows };
    },
    staleTime: 5 * 60 * 1000,
  });
  const facetRows = facetQuery.data?.rows ?? [];

  const columnValue = (r: T, c: MRT_ColumnDef<T>) =>
    c.accessorFn
      ? (c.accessorFn as (row: T) => any)(r)
      : r[c.accessorKey as string];
  // A column is filterable only if its values are primitives — an array/object
  // column (e.g. Syndication's `lenders`) can't be sensibly checkbox-filtered.
  const isFilterable = (c: MRT_ColumnDef<T>): boolean => {
    if (c.enableColumnFilter === false) return false;
    if (!c.accessorKey && !c.accessorFn) return false;
    // The platform's grids are server-paged, so a facet is honest only if the register
    // can apply it — a column with no filterParam mapping gets no funnel rather than a
    // checkbox that silently filters nothing. (Mock mode filters client-side and keeps
    // every primitive column filterable, as before.)
    if (USE_REAL_API && !filterParamOf(c)) return false;
    const sample = facetRows
      .map((r) => columnValue(r, c))
      .find((v) => v !== undefined && v !== null);
    return typeof sample !== "object";
  };
  const serverFilters = useMemo(
    () =>
      Object.entries(colFilters)
        .filter(([, v]) => v && v.length)
        .map(([id, values]) => {
          const col = columns.find((c) => String(c.id ?? c.accessorKey) === id);
          const param = col ? filterParamOf(col) : undefined;
          if (!col || !param) return null;
          // A JOINED column (Company) displays a name the server cannot filter by; its
          // meta.filterRowValue reads the row's underlying key (entity_id) instead, so
          // the facet stays on names while the request carries ids.
          const rowValue = (col.meta as any)?.filterRowValue as ((r: T) => string) | undefined;
          const out = rowValue
            ? values
                .map((display) => {
                  const hit = facetRows.find((r) => String(columnValue(r, col)) === display);
                  return hit ? String(rowValue(hit) || "") : "";
                })
                .filter(Boolean)
            : values;
          return out.length ? { param, values: out } : null;
        })
        .filter((x): x is { param: string; values: string[] } => x !== null),
    [colFilters, columns, facetRows],
  );

  const distinctValues = (c: MRT_ColumnDef<T>): string[] => {
    const seen = new Set<string>();
    facetRows.forEach((r) => {
      const v = columnValue(r, c);
      if (v === undefined || v === null || v === "" || typeof v === "object") return;
      // Split comma-separated multi-value cells (e.g. stacked Roles, multi-sector) so the
      // filter offers each token individually rather than the combined string.
      String(v)
        .split(/,\s*/)
        .map((s) => s.trim())
        .filter(Boolean)
        .forEach((tok) => seen.add(tok));
    });
    return Array.from(seen)
      .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
      ;
  };

  // The phone has no column headers to hang a funnel on, so the same per-column filters
  // gather into one sheet behind the search bar's tune icon — the filtering the grid
  // offers is still reachable, just relocated. Built only on mobile: distinctValues walks
  // the whole facet set and the grid already does that once.
  const filterableCols = useMemo(
    () => (isMobile
      ? columns.filter(isFilterable).map((c) => ({
          id: String(c.id ?? c.accessorKey),
          label: String(c.header ?? ""),
          options: distinctValues(c),
        }))
      : []),
    [isMobile, columns, facetRows],
  );

  // The phone search box types into a draft and commits on a pause. MRT's own search
  // field debounces before it calls onGlobalFilterChange; this one has to do the same, or
  // every keystroke resets the feed and fires a fresh page-0 request.
  const [searchDraft, setSearchDraft] = useState("");
  useEffect(() => {
    const id = window.setTimeout(() => setGlobalFilter(searchDraft), 300);
    return () => window.clearTimeout(id);
  }, [searchDraft]);

  const [sheetOpen, setSheetOpen] = useState(false);
  const [sheetCol, setSheetCol] = useState<string | null>(null);
  const closeSheet = () => { setSheetOpen(false); setSheetCol(null); };
  const activeFilters = Object.keys(colFilters).length;

  // Exports cover every data column — keyed or computed — for the current page's rows.
  const exportCols = () =>
    columns.filter((c) => c.accessorKey || typeof c.accessorFn === "function");
  const cellText = (r: T, c: MRT_ColumnDef<T>) => {
    const v = c.accessorFn ? c.accessorFn(r) : r[c.accessorKey as string];
    return String(v ?? "");
  };

  const saveCsv = (csv: string, name: string) => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
  };

  const downloadCsv = () => {
    const cols = exportCols();
    const header = cols.map((c) => c.header);
    const body = rows.map((r) =>
      cols.map((c) => `"${cellText(r, c).replace(/"/g, '""')}"`).join(","),
    );
    saveCsv([header.join(","), ...body].join("\n"), `${csvName}.csv`);
  };

  // v12 parity: every actions column carries "export this row as a one-line CSV"
  // (header row + the row's values, across all exported columns).
  const downloadRowCsv = (r: T, idx: number) => {
    const cols = exportCols();
    const q = (v: string) => `"${v.replace(/"/g, '""')}"`;
    const csv =
      cols.map((c) => q(String(c.header ?? ""))).join(",") +
      "\n" +
      cols.map((c) => q(cellText(r, c))).join(",") +
      "\n";
    const slug = String(r.code ?? r.id ?? idx).replace(/[^A-Za-z0-9_-]+/g, "_");
    saveCsv(csv, `${csvName}_${slug}.csv`);
  };

  // No PDF library in the bundle — render a clean print sheet and let the browser
  // "Save as PDF". Same columns/rows as the CSV export.
  const downloadPdf = () => {
    const cols = exportCols();
    const esc = (s: string) =>
      s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    const title = csvName.replace(/[_-]+/g, " ");
    const head = cols.map((c) => `<th>${esc(String(c.header))}</th>`).join("");
    const body = rows
      .map(
        (r) =>
          `<tr>${cols.map((c) => `<td>${esc(cellText(r, c))}</td>`).join("")}</tr>`,
      )
      .join("");
    const w = window.open("", "_blank");
    if (!w) return; // popup blocked
    w.document.write(
      `<!doctype html><html><head><title>${esc(title)}</title><style>
        body{font:12px/1.4 system-ui,sans-serif;margin:24px;color:#12333B}
        h1{font-size:16px;margin:0 0 12px}
        table{border-collapse:collapse;width:100%}
        th,td{border:1px solid #D7E3E6;padding:5px 7px;text-align:left;vertical-align:top}
        th{background:#F7FAFB;font-size:10.5px;text-transform:uppercase;letter-spacing:.5px}
        tr{break-inside:avoid}
        @page{size:landscape;margin:12mm}
      </style></head><body><h1>${esc(title)}</h1>
      <table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></body></html>`,
    );
    w.document.close();
    w.focus();
    w.print();
  };

  // Each column gets v12's filter popover (contains box + checkbox list) unless the
  // page turns filtering off. The last data column also absorbs any leftover width
  // so the table fills its container.
  const sizedColumns = useMemo(
    () =>
      columns.map((c, i) => {
        const last = i === columns.length - 1;
        const canFilter = isFilterable(c);
        const options = canFilter ? distinctValues(c) : [];
        return {
          ...c,
          grow: last,
          // Own the header content — v12's label + funnel. MRT still supplies the
          // native sort icon around it; our funnel drives our own filter state.
          Header: ({ column }: any) => (
            <FilterHeader
              colId={column.id}
              label={String(c.header ?? "")}
              options={options}
              canFilter={canFilter}
            />
          ),
        };
      }),
    // facetRows drives the option lists; recompute when they arrive.
    [columns, facetRows],
  );

  // The row's icon set — the grid's pinned Actions column and the phone card's footer
  // render THIS, so Edit on a card is the same button, the same handler and the same
  // permission gate as Edit in the grid. `justify` is the only thing that differs: the
  // column centres its icons, the card pushes them to its trailing edge.
  const rowActions = (row: T, index: number, justify: "center" | "flex-end" = "center") => (
    <Stack
      direction="row"
      spacing={0}
      justifyContent={justify}
      alignItems="center"
      sx={{
        width: "100%",
        // Icons are packed tight so up to four fit the "Actions"-width column,
        // and the glyphs are shrunk a touch below MUI's "small" default.
        "& .MuiIconButton-root": { p: "2px" },
        "& .MuiSvgIcon-root": { fontSize: 16 },
      }}
      onClick={(e) => e.stopPropagation()}
    >
      {/* Page-supplied action(s) lead the row (e.g. Leads' Push to deal). */}
      {extraActions?.(row)}
      {onView && (
        <Tooltip title="View">
          <IconButton color="primary" onClick={() => onView(row)}>
            <VisibilityIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      )}
      {onEdit && (
        // `editReason` means "offered, but not by you" — rendered disabled with the
        // reason rather than vanishing, so a user can tell a permission apart from a
        // bug. A disabled MUI button swallows pointer events, hence the span.
        <Tooltip title={editReason || 'Edit'}>
          <span>
            <IconButton disabled={!!editReason}
              onClick={() => !editReason && onEdit(row)}>
              <EditIcon fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
      )}
      <Tooltip title="Export row as CSV">
        <IconButton onClick={() => downloadRowCsv(row, index)}>
          <FileDownloadIcon fontSize="small" />
        </IconButton>
      </Tooltip>
      {onDelete && (
        <Tooltip title="Delete">
          <IconButton color="error" onClick={() => onDelete(row)}>
            <DeleteIcon fontSize="small" />
          </IconButton>
        </Tooltip>
      )}
    </Stack>
  );

  // ---- Hidden-column navigation (desktop) -------------------------------------
  // A wide grid scrolls horizontally beneath the pinned Actions tray. Two quiet
  // chevrons make the hidden columns one click away: › floats at the tray's edge
  // while columns are hidden to the right, ‹ at the grid's left edge once scrolled.
  // Both disappear the moment everything fits — a wide screen never sees them.
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const [colNav, setColNav] = useState({ left: false, right: false, tray: 0 });
  useEffect(() => {
    const sc = scrollerRef.current;
    if (!sc || isMobile) return;
    const update = () => {
      const tray = sc.querySelector("thead th:last-child") as HTMLElement | null;
      const max = sc.scrollWidth - sc.clientWidth;
      const next = {
        left: sc.scrollLeft > 4,
        right: sc.scrollLeft < max - 4,
        tray: tray?.offsetWidth ?? 0,
      };
      setColNav((prev) =>
        prev.left === next.left && prev.right === next.right && prev.tray === next.tray
          ? prev
          : next);
    };
    update();
    sc.addEventListener("scroll", update, { passive: true });
    const ro = new ResizeObserver(update);
    ro.observe(sc);
    return () => { sc.removeEventListener("scroll", update); ro.disconnect(); };
  }, [isMobile, rows, columns]);
  const scrollColumns = (toEnd: boolean) => {
    const sc = scrollerRef.current;
    if (!sc) return;
    sc.scrollTo({ left: toEnd ? sc.scrollWidth : 0, behavior: "smooth" });
  };
  const colArrowSx = {
    position: "absolute" as const,
    top: "48%",
    zIndex: 4,
    width: 30,
    height: 30,
    bgcolor: "rgba(255,255,255,.95)",
    color: tokens.teal,
    border: "1.5px solid #B9C9CF",
    boxShadow: "0 2px 7px rgba(10,40,45,.22)",
    "&:hover": { bgcolor: "#EEF6F4", borderColor: tokens.teal },
  };

  const table = useMaterialReactTable<T>({
    columns: sizedColumns,
    layoutMode: "grid",
    // Floor on column width so the header label + native sort icon + our funnel
    // always have room and never overlap (a page's larger `size` still wins).
    defaultColumn: { minSize: 112 },
    data: rows,
    manualPagination: true,
    manualSorting: true,
    // Filtering is entirely ours (own state + header funnel). Turning off MRT's
    // column filters means it renders no filter chrome at all — no subheader row,
    // no filter icons, no "filtered" indicator. The ⋮ actions menu is off too, so
    // the header is just MRT's native sort icon plus our funnel.
    enableColumnFilters: false,
    enableColumnActions: false,
    rowCount: total,
    state: {
      pagination,
      sorting,
      globalFilter,
      // Loaders disabled — no progress bar or skeleton overlay. placeholderData keeps
      // the previous rows on screen during a refetch, so the table never flashes empty.
      showProgressBars: false,
      isLoading: false,
    },
    onPaginationChange: setPagination,
    onSortingChange: setSorting,
    onGlobalFilterChange: (updater) =>
      setGlobalFilter((prev) => {
        const next =
          typeof updater === "function"
            ? (updater as (p: string) => string)(prev)
            : updater;
        return next ?? "";
      }),

    enableColumnResizing: true,
    enableColumnPinning: true,
    enableStickyHeader: true,
    enableGlobalFilter: true, // the search toggle reveals a per-table search box
    muiSearchTextFieldProps: {
      placeholder: "Search this table…",
      sx: { minWidth: 180 },
    },
    enableDensityToggle: false,
    positionActionsColumn: "last",
    enableRowActions: actionsEnabled,
    initialState: {
      density: "compact",
      showColumnFilters: false,
      // Action column is pinned (sticky) to the right. Its MRT opaque cover would hide
      // the zebra stripe, so the body-row sx below repaints every cell background with
      // !important — that reaches the pinned cell too, so it stays striped and uniform.
      columnPinning: { right: actionsEnabled ? ["mrt-row-actions"] : [] },
    },
    displayColumnDefOptions: {
      "mrt-row-actions": {
        header: "Actions",
        // Column hugs the icon row — the "Actions"-word width normally, a little
        // wider when a page supplies an extra action.
        size: actionsColSize,
        minSize: actionsColSize,
        grow: false,
        enableResizing: false,
        // Same v12 `th` type as every other header — the column-level sx replaces
        // the table-level one, so the type rules are repeated here rather than inherited.
        muiTableHeadCellProps: {
          align: "center",
          sx: {
            "&.MuiTableCell-root": { backgroundColor: V12_HEAD_BG },
            bgcolor: V12_HEAD_BG,
            color: tokens.muted,
            fontSize: 10.6,
            fontWeight: 700,
            textTransform: "uppercase",
            letterSpacing: ".5px",
            borderBottom: `1px solid ${tokens.line}`,
            whiteSpace: "nowrap",
            "& .Mui-TableHeadCell-Content": { justifyContent: "center" },
          },
        },
        muiTableBodyCellProps: {
          align: "center",
          sx: {
            px: 0.5,
            // Same boundary as the header: the tray is a surface, not an overlap.
            borderLeft: "2px solid #C9D4DA",
            boxShadow: "-8px 0 10px -6px rgba(15,40,50,.25)",
          },
        },
      },
    },
    // MRT paints the head row (and again for the sticky header) over the cell, so the
    // v12 header colour has to be set here too or the cell rule never shows.
    muiTableHeadProps: {
      sx: { "& .MuiTableRow-root": { bgcolor: V12_HEAD_BG } },
    },
    muiTableHeadRowProps: { sx: { bgcolor: V12_HEAD_BG, boxShadow: "none" } },

    // requirement 11: sort + filter affordances sit on the right of each header.
    // Type matches v12's `th`: uppercase, 10.6px, muted on #F7F9FA, single line.
    muiTableHeadCellProps: {
      align: "left",
      sx: {
        "&.MuiTableCell-root": {
          backgroundColor: V12_HEAD_BG,
        },
        bgcolor: V12_HEAD_BG,
        color: tokens.muted,
        fontSize: 10.6,
        fontWeight: 700,
        textTransform: "uppercase",
        letterSpacing: ".5px",
        borderBottom: `1px solid ${tokens.line}`,
        // v12 keeps every header on one line — no wrapping, no ellipsis.
        whiteSpace: "nowrap",
        // MRT renders its own filter icon in the Content-Actions slot; we use our
        // own funnel (in the header content) instead, so hide MRT's. The native
        // sort icon lives in Content-Labels and is kept.
        "& .Mui-TableHeadCell-Content-Actions": { display: "none" },
        // ---- Header right-side budget (label · funnel · sort arrow · resize grip) ----
        // MRT lays the cell out as:
        //   Content(position:relative, flex row)
        //     Content-Labels(flex row) → Content-Wrapper(our label+funnel) + sort arrow
        //     ResizeHandle(position:absolute, right:0 of Content)  ← the "separator"
        // Reserve 10px on Content's right so no in-flow icon reaches the resize grip.
        "& .Mui-TableHeadCell-Content": {
          flexDirection: "row",
          width: "100%",
          pr: "10px",
        },
        // Wrapper (label+funnel) and the sort arrow are siblings here; a 6px row gap
        // keeps the funnel clear of the sort arrow. flex:1 lets the label truncate.
        "& .Mui-TableHeadCell-Content-Labels": {
          flexDirection: "row",
          flex: 1,
          minWidth: 0,
          overflow: "visible",
          gap: "6px",
        },
        // Wrapper takes leftover width; the label inside it (not the funnel) truncates.
        "& .Mui-TableHeadCell-Content-Wrapper": {
          flex: 1,
          minWidth: 0,
          whiteSpace: "nowrap",
          overflow: "visible",
          maxWidth: "unset",
        },
        // 18px sort arrow, always visible (faint when inactive), fixed size — no margins:
        // the 6px Labels gap sits to its left, the 10px Content padding to its right.
        "& .MuiTableSortLabel-root": { flexShrink: 0 },
        "& .MuiTableSortLabel-icon": { opacity: 0.45, fontSize: 18, margin: 0 },
        "& .MuiTableSortLabel-root.Mui-active .MuiTableSortLabel-icon": {
          opacity: 1,
          color: "#0D7377",
        },
        // Thin, crisp column resize grip.
        "& .Mui-TableHeadCell-ResizeHandle": {
          opacity: 1,
          borderRightWidth: "1px",
          borderColor: "#B9C6CB",
        },
      },
    },

    // No internal VERTICAL scroll — the table grows to fit its paginated rows
    // (maxHeight:none) so no vertical scrollbar ever appears. The HORIZONTAL scrollbar
    // is left visible so wide tables can be scrolled sideways.
    muiTableContainerProps: {
      ref: scrollerRef,
      sx: { maxHeight: "none" },
    },
    muiTableBodyCellProps: {
      sx: {
        fontSize: 12.6,
        padding: "7px 9px",
        borderBottom: "1px solid #EFF2F4",
        verticalAlign: "middle",
        // Long content wraps onto new lines (and long unbroken strings break) instead
        // of overflowing on one line. Columns can still force nowrap per-column.
        whiteSpace: "normal",
        wordBreak: "break-word",
        overflowWrap: "anywhere",
      },
    },

    // Compact top toolbar: short, small icons, vertically centred.
    muiTopToolbarProps: {
      sx: {
        minHeight: 36,
        "& .MuiBox-root": { alignItems: "center" },
        "& .MuiIconButton-root": { padding: "4px" },
        "& .MuiSvgIcon-root": { fontSize: 18 },
      },
    },
    // Compact pagination. Every text node forced to 10px (!important beats the
    // per-class rules like .MuiTablePagination-selectLabel); icons overridden back
    // up to 16px. No negative margins — they break the horizontal alignment.
    muiBottomToolbarProps: {
      sx: {
        minHeight: 30,
        display: "flex",
        alignItems: "center",
        "& *": { fontSize: "12.5px !important", lineHeight: 1.2 },
        "& .MuiSvgIcon-root": { fontSize: "19px !important" },
        "& .MuiIconButton-root": { padding: "3px" },
        // Pull the whole pagination block up (moves as one, so it stays aligned).
        "& .MuiTablePagination-root": { overflow: "visible", mt: "-9px" },
        "& .MuiTablePagination-toolbar": {
          minHeight: 30,
          alignItems: "center",
          display: "flex",
        },
        "& .MuiTablePagination-spacer": { flex: "1 1 100%" },
        "& .MuiTablePagination-selectLabel, & .MuiTablePagination-displayedRows":
          { m: 0 },
        "& .MuiTablePagination-input, & .MuiTablePagination-select": {
          m: "0 4px",
        },
        "& .MuiTablePagination-actions": { ml: 1 },
      },
    },
    muiPaginationProps: {
      rowsPerPageOptions: [10, 25, 50, 100],
      // Cursor paging can only walk forward one page at a time — the cursor for an
      // unvisited page isn't known yet — so First/Last are offered only when the source
      // supports jumping. Prev still works: those cursors have been seen.
      showFirstButton: !cursorMode,
      showLastButton: !cursorMode,
      // The rows-per-page dropdown menu renders in a portal, so the toolbar sx can't
      // reach it — size and centre its options here.
      SelectProps: {
        MenuProps: {
          sx: {
            "& .MuiMenuItem-root": {
              fontSize: 12.5,
              minHeight: 30,
              justifyContent: "center",
            },
          },
        },
      },
    },
    // Zebra striping on the body, with v12's #F4FAF9 row hover on top of it.
    // (v12 itself is unstriped — plain white rows — so this is an addition.)
    muiTableBodyRowProps: ({ row }) => {
      // A per-row override (v12 gray/rowok/rowbad) wins over the zebra stripe — but ONLY
      // when it actually carries styling. An empty {} (AM's non-Dropped rows, Lending's
      // uncoloured stages) must still fall back to the zebra, or those tables look flat.
      const custom = rowSx?.(row.original);
      const hasCustom = !!custom && Object.keys(custom).length > 0;
      return {
        hover: true,
        ...(onRowClick ? { onClick: () => onRowClick(row.original) } : {}),
        sx: {
          cursor: onRowClick ? "pointer" : "default",
          // Backgrounds are !important so they also paint the pinned/sticky action cell,
          // whose MRT opaque cover would otherwise hide the stripe. A stage-coloured row
          // keeps its colour and skips the zebra; the hover tint wins over both.
          ...(hasCustom
            ? { "& > td": custom }
            : {
                "& > td": { backgroundColor: "#fff !important" },
                "&:nth-of-type(even) > td": { backgroundColor: "#FAFBFC !important" },
              }),
          "&:hover > td": { backgroundColor: "#F4FAF9 !important" },
        },
      };
    },

    renderRowActions: actionsEnabled
      ? ({ row }) => rowActions(row.original, row.index)
      : undefined,

    // Toolbar: clear filters, global search toggle, columns toggle, fullscreen,
    // downloads. Icons carry no per-icon fontSize so they inherit the toolbar's
    // 18px SvgIcon size and match MRT's built-in buttons.
    renderToolbarInternalActions: ({ table }) => (
      <Box sx={{ display: "flex", alignItems: "center" }}>
        <Tooltip title="Clear filters">
          <IconButton onClick={() => setColFilters({})}>
            <BlockSharpIcon fontSize="small" />
          </IconButton>
        </Tooltip>
        <MRT_ToggleGlobalFilterButton table={table} />
        <MRT_ShowHideColumnsButton table={table} />
        <MRT_ToggleFullScreenButton table={table} />
        <Tooltip title="Download CSV">
          <IconButton onClick={downloadCsv}>
            <DownloadIcon />
          </IconButton>
        </Tooltip>
        <Tooltip title="Download PDF">
          <IconButton onClick={downloadPdf}>
            <PictureAsPdfIcon />
          </IconButton>
        </Tooltip>
      </Box>
    ),
    renderTopToolbarCustomActions: toolbarLeft
      ? () => <>{toolbarLeft}</>
      : undefined,

    enableExpanding: enableExpand,
  });

  // ---- Phone: the same rows, the same query, the same handlers — read as cards ----
  if (isMobile) {
    // A page that hasn't named a headline still gets one: its lead column.
    const card: MobileCardSpec<T> = mobileCard ?? {
      primary: (r) => String(columnValue(r, columns[0]) ?? ""),
    };
    const sheetColDef = filterableCols.find((c) => c.id === sheetCol);

    // Every column, rendered by its OWN `Cell` so the card shows exactly what the grid
    // cell shows — the coloured pill, the code chip, the live stage <select> — not a
    // flattened string of it. MRT hands `Cell` a large context object; these tables read
    // only getValue / row.original / row.index from it, so a stand-in carrying those
    // renders them faithfully off-grid. (Called as a plain function, not mounted as a
    // component: a `Cell` that used hooks would need a real component here.)
    const cellNode = (r: T, c: MRT_ColumnDef<T>, index: number): React.ReactNode => {
      const value = columnValue(r, c);
      const Cell = c.Cell as any;
      if (typeof Cell === "function") {
        const id = String(c.id ?? c.accessorKey ?? "");
        return Cell({
          cell: { getValue: () => value, id, row: { original: r, index } },
          row: { original: r, index, id: String(index) },
          column: { id, columnDef: c },
          table,
          renderedCellValue: value ?? "",
        });
      }
      return value === undefined || value === null ? "" : String(value);
    };

    return (
      <FilterContext.Provider value={filterCtx}>
        <Box sx={{ minWidth: 0, maxWidth: "100%", overflowX: "clip" }}>
          {toolbarLeft && (
            <Box sx={{ display: "flex", flexWrap: "wrap", gap: 1, alignItems: "center", mb: 1 }}>
              {toolbarLeft}
            </Box>
          )}

          <Paper elevation={0}
            sx={{ display: "flex", alignItems: "center", gap: "8px", px: "12px", py: "8px",
              border: `1px solid ${tokens.line}`, borderRadius: "14px", bgcolor: "#fff", mb: 1 }}>
            <SearchIcon sx={{ fontSize: 20, color: tokens.muted }} />
            <InputBase
              value={searchDraft}
              onChange={(e) => setSearchDraft(e.target.value)}
              placeholder="Search"
              sx={{ flex: 1, fontSize: 14.5, minWidth: 0 }}
            />
            <Typography sx={{ fontSize: 13, color: tokens.muted, whiteSpace: "nowrap" }}>
              {feedRows.length}{cursorMode || !total ? "" : `/${total}`}
            </Typography>
            <IconButton size="small" aria-label="Filters" onClick={() => setSheetOpen(true)}>
              <Badge badgeContent={activeFilters} color="primary"
                sx={{ "& .MuiBadge-badge": { fontSize: 9, height: 14, minWidth: 14 } }}>
                <TuneIcon sx={{ fontSize: 20, color: activeFilters ? "#0D7377" : tokens.ink }} />
              </Badge>
            </IconButton>
          </Paper>

          {/* minmax(0,1fr): a bare `1fr` track floors at its content's min-content width,
              so one long unbroken value would push the whole card wider than the screen. */}
          <Box sx={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr)", gap: 1 }}>
            {feedRows.map((r, i) => (
              <Paper
                key={String(r.id ?? r.code ?? i)}
                variant="outlined"
                onClick={onRowClick ? () => onRowClick(r) : undefined}
                sx={{
                  borderColor: tokens.line, borderRadius: "12px", px: 1.4, py: 1.2,
                  minWidth: 0, maxWidth: "100%",
                  cursor: onRowClick ? "pointer" : "default",
                  "&:active": { bgcolor: "#F4FAF9" },
                  // A page's row state (v12 gray/rowok/rowbad) tints the whole card here,
                  // which is what it tints per-cell in the grid.
                  ...(rowSx?.(r) ?? {}),
                }}
              >
                <Box sx={{ display: "flex", alignItems: "flex-start", gap: "10px", minWidth: 0 }}>
                  {/* Title truncates rather than wraps — the untruncated value is in the
                      field list directly below, so nothing is actually lost. */}
                  <Box sx={{ flex: 1, minWidth: 0, fontSize: 15, fontWeight: 600,
                    lineHeight: 1.3, color: tokens.ink,
                    overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {card.primary(r)}
                  </Box>
                  {card.value && (
                    <Box sx={{ textAlign: "right", flexShrink: 1, maxWidth: "48%", minWidth: 0,
                      fontSize: 15, lineHeight: 1.3, color: tokens.ink,
                      overflowWrap: "anywhere" }}>
                      {card.value(r)}
                    </Box>
                  )}
                </Box>

                {/* The whole row, field by field — the card carries everything the grid
                    column set carries, so nothing is only-on-desktop. */}
                <Box sx={{ mt: 1, pt: 1, borderTop: `1px solid ${tokens.line}`,
                  display: "grid", gridTemplateColumns: "minmax(76px, 34%) minmax(0, 1fr)",
                  columnGap: 1.2, rowGap: 0.9, alignItems: "baseline" }}>
                  {columns.map((c, ci) => (
                    <Fragment key={String(c.id ?? c.accessorKey ?? ci)}>
                      <Box sx={{ fontSize: 10.6, fontWeight: 700, textTransform: "uppercase",
                        letterSpacing: ".5px", color: tokens.muted,
                        minWidth: 0, overflowWrap: "anywhere" }}>
                        {String(c.header ?? "")}
                      </Box>
                      <Box sx={{
                        fontSize: 12.8, minWidth: 0, maxWidth: "100%",
                        wordBreak: "break-word", overflowWrap: "anywhere",
                        // Cells are the grid's own renderers, sized for a wide column: a
                        // fixed-width <select>, a nowrap pill, a row of chips. Inside a
                        // phone card they have to give way to the card, not the reverse.
                        "& > *": { maxWidth: "100%" },
                        "& .MuiTextField-root, & .MuiInputBase-root": {
                          minWidth: 0, width: "100%", maxWidth: "100%",
                        },
                        "& .MuiStack-root": { flexWrap: "wrap" },
                        "& .MuiChip-root": { maxWidth: "100%" },
                      }}>
                        {cellNode(r, c, i)}
                      </Box>
                    </Fragment>
                  ))}
                </Box>

                {/* The grid's Actions column, on the card. Same buttons, same handlers —
                    allowed to wrap here, where a page's extra actions can outrun the
                    width the pinned column always had. */}
                {actionsEnabled && (
                  <Box sx={{ mt: 1, pt: 0.8, borderTop: `1px solid ${tokens.line}`,
                    "& .MuiStack-root": { flexWrap: "wrap" } }}>
                    {rowActions(r, i, "flex-end")}
                  </Box>
                )}
              </Paper>
            ))}
          </Box>

          {/* Tripwire for the next page, plus whatever the list is doing right now. */}
          <Box ref={sentinel} sx={{ height: 1 }} />
          <Box sx={{ py: 1.5, textAlign: "center", fontSize: 12.4, color: tokens.muted }}>
            {query.isFetching
              ? "Loading…"
              : feedRows.length === 0
                ? "No records"
                : hasMore ? "" : "End of list"}
          </Box>

          <Drawer anchor="bottom" open={sheetOpen} onClose={closeSheet}
            PaperProps={{ sx: { borderTopLeftRadius: 16, borderTopRightRadius: 16,
              maxHeight: "72vh", pb: "env(safe-area-inset-bottom)" } }}>
            {sheetColDef ? (
              <Box>
                <Box sx={{ display: "flex", alignItems: "center", gap: 0.5, px: 1, py: 1,
                  borderBottom: `1px solid ${tokens.line}` }}>
                  <IconButton size="small" aria-label="Back" onClick={() => setSheetCol(null)}>
                    <ArrowBackIcon fontSize="small" />
                  </IconButton>
                  <Typography sx={{ fontSize: 13.5, fontWeight: 700 }}>{sheetColDef.label}</Typography>
                </Box>
                <FilterBody
                  value={colFilters[sheetColDef.id]}
                  options={sheetColDef.options}
                  onClose={() => setSheetCol(null)}
                  onChange={(v) => filterCtx.set(sheetColDef.id, v)}
                />
              </Box>
            ) : (
              <Box>
                <Box sx={{ display: "flex", alignItems: "center", px: 1.75, py: 1.25,
                  borderBottom: `1px solid ${tokens.line}` }}>
                  <Typography sx={{ fontSize: 13.5, fontWeight: 700, flex: 1 }}>Filters</Typography>
                  {activeFilters > 0 && (
                    <Button size="small" onClick={() => setColFilters({})}
                      sx={{ fontSize: 12, textTransform: "none" }}>Clear all</Button>
                  )}
                </Box>
                <Box sx={{ overflowY: "auto" }}>
                  {filterableCols.length === 0 && (
                    <Typography sx={{ px: 1.75, py: 1.5, fontSize: 12.5, color: tokens.muted }}>
                      Nothing to filter on this table.
                    </Typography>
                  )}
                  {filterableCols.map((c) => {
                    const on = colFilters[c.id]?.length ?? 0;
                    return (
                      <MenuItem key={c.id} onClick={() => setSheetCol(c.id)}
                        sx={{ py: 1.15, fontSize: 13.4 }}>
                        <Box sx={{ flex: 1 }}>{c.label}</Box>
                        {on > 0 && (
                          <Box sx={{ fontSize: 11.5, fontWeight: 700, color: "#0D7377" }}>{on}</Box>
                        )}
                      </MenuItem>
                    );
                  })}
                  {/* The toolbar's exports have no home on a phone; they live here. */}
                  <Divider />
                  <MenuItem onClick={() => { closeSheet(); downloadCsv(); }} sx={{ py: 1.15, fontSize: 13.4 }}>
                    <ListItemIcon><DownloadIcon fontSize="small" /></ListItemIcon>
                    Download CSV
                  </MenuItem>
                  <MenuItem onClick={() => { closeSheet(); downloadPdf(); }} sx={{ py: 1.15, fontSize: 13.4 }}>
                    <ListItemIcon><PictureAsPdfIcon fontSize="small" /></ListItemIcon>
                    Download PDF
                  </MenuItem>
                </Box>
              </Box>
            )}
          </Drawer>
        </Box>
      </FilterContext.Provider>
    );
  }

  // Provider so each header's funnel can read/write our column-filter state.
  return (
    <FilterContext.Provider value={filterCtx}>
      <Box sx={{ position: "relative" }}>
        <MaterialReactTable table={table} />
        {colNav.right && (
          <IconButton
            aria-label="Show the remaining columns"
            onClick={() => scrollColumns(true)}
            sx={{ ...colArrowSx, right: colNav.tray + 10 }}
            size="small"
          >
            <ChevronRightIcon fontSize="small" />
          </IconButton>
        )}
        {colNav.left && (
          <IconButton
            aria-label="Back to the first columns"
            onClick={() => scrollColumns(false)}
            sx={{ ...colArrowSx, left: 10 }}
            size="small"
          >
            <ChevronLeftIcon fontSize="small" />
          </IconButton>
        )}
      </Box>
    </FilterContext.Provider>
  );
}
