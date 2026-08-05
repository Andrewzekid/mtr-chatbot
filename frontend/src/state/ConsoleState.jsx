import { createContext, useContext, useState, useEffect, useCallback } from 'react';
import { getJSON } from '../api/client';

const ConsoleStateContext = createContext(null);

const INITIAL_DB = {
  db_available: false,
  categories: [],
  inspections: [],
  category_counts: [],
  total_objects: 0,
  anomaly_tables: false,
  time_range: null,
  rerun: { enabled: false, listening: false, job_stats: null },
};

export function ConsoleStateProvider({ children }) {
  const [db, setDb] = useState(INITIAL_DB);
  const [inspectionId, setInspectionId] = useState(null);
  const [timeRange, setTimeRange] = useState({ start: '', end: '' });
  const [highlightLog, setHighlightLog] = useState([]);
  const [dbRefreshTick, setDbRefreshTick] = useState(0);

  const refreshDb = useCallback(async () => {
    try {
      const info = await getJSON('/api/info');
      setDb(info);
    } catch (e) { console.warn('db info fetch failed', e); }
  }, []);

  const refreshRerun = useCallback(async () => {
    try {
      const st = await getJSON('/api/rerun/status');
      setDb(prev => ({ ...prev, rerun: st }));
    } catch {}
  }, []);

  useEffect(() => { refreshDb(); }, [refreshDb, dbRefreshTick]);
  useEffect(() => {
    const id = setInterval(refreshRerun, 10000);
    return () => clearInterval(id);
  }, [refreshRerun]);

  const addHighlightLog = useCallback((entry) => {
    setHighlightLog(prev => [{ ts: Date.now(), ...entry }, ...prev].slice(0, 50));
  }, []);

  return (
    <ConsoleStateContext.Provider value={{
      db, inspectionId, setInspectionId,
      timeRange, setTimeRange,
      highlightLog, addHighlightLog,
      refreshDb, refreshRerun, setDbRefreshTick,
    }}>
      {children}
    </ConsoleStateContext.Provider>
  );
}

export function useConsoleState() {
  const ctx = useContext(ConsoleStateContext);
  if (!ctx) throw new Error('useConsoleState must be inside ConsoleStateProvider');
  return ctx;
}
