import { db } from '../api/atlasStore';
import { writeAudit } from './auditService';
import { localMinute } from '../api/time';

export interface Note { when: string; by: string; text: string; }

export const notesService = {
  for(code: string): Note[] { return (db().notes && db().notes[code]) || []; },
  add(code: string, text: string, by: string) {
    const d: any = db(); if (!d.notes) d.notes = {};
    (d.notes[code] = d.notes[code] || []).unshift({ when: localMinute(new Date().toISOString()), by, text });
    writeAudit(by, 'Update added', code, text.slice(0, 80));
  },
  auditFor(code: string): any[] { return (db().audit || []).filter((a: any) => a.code === code).slice(0, 6); },
};
