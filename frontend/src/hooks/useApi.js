import { useState, useEffect, useCallback } from 'react';
import { getJSON } from '../api/client';

export function useApi(path, opts = {}) {
  const { deps = [], enabled = true } = opts;
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const d = await getJSON(path);
      setData(d);
    } catch (e) {
      setError(e.message || 'fetch failed');
    } finally {
      setLoading(false);
    }
  }, [path, ...deps]);

  useEffect(() => {
    if (enabled) refresh();
    else { setLoading(false); }
  }, [enabled, refresh]);

  return { data, loading, error, refresh };
}
