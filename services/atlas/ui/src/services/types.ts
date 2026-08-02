import type { MRT_ColumnFiltersState, MRT_SortingState } from 'material-react-table';

export interface TableQuery {
  pageIndex: number;
  pageSize: number;
  globalFilter?: string;
  sorting?: MRT_SortingState;
  columnFilters?: MRT_ColumnFiltersState;
  searchFields?: string[];
  /**
   * Cursor for the page being asked for — the `next_cursor` the previous page returned.
   * Absent on the first page. Only the cursor-paged endpoints read it.
   */
  cursor?: string;
}

export interface Paged<T> {
  rows: T[];
  total: number;
  /**
   * Cursor that fetches the page AFTER this one; null on the last page. Present only
   * from cursor-paged sources — its presence is what puts the table in cursor mode,
   * where pages are walked with next/prev rather than jumped to.
   */
  nextCursor?: string | null;
}
