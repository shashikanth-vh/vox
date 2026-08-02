import { createContext, useContext, useState, type ReactNode } from 'react';

const Ctx = createContext<{ search: string; setSearch: (s: string) => void } | null>(null);

export function SearchProvider({ children }: { children: ReactNode }) {
  const [search, setSearch] = useState('');
  return <Ctx.Provider value={{ search, setSearch }}>{children}</Ctx.Provider>;
}

// Requirement 15: navbar search feeds whichever page table is active.
export function useSearch() {
  const c = useContext(Ctx);
  if (!c) throw new Error('useSearch within SearchProvider');
  return c;
}
