import { useState } from 'react';
import { useApi } from '../hooks/useApi';
import { useConsoleState } from '../state/ConsoleState';
import ObjectGrid from '../components/ObjectGrid';
import { apiURL } from '../api/client';

const PRESETS = [
  { label: 'All', start: '', end: '' },
  { label: '16:50–17:00', start: '16:50', end: '17:00' },
  { label: '16:55–16:58', start: '16:55', end: '16:58' },
  { label: 'Last 15 min', start: '16:45', end: '17:00' },
];

export default function TimeExplorer() {
  const { inspectionId } = useConsoleState();
  const [start, setStart] = useState('16:50');
  const [end, setEnd] = useState('17:00');
  const [query, setQuery] = useState(null);
  const [images, setImages] = useState(null);

  const iidParam = inspectionId ? `&inspection_id=${inspectionId}` : '';

  const runQuery = async () => {
    if (!start || !end) return;
    setQuery(`/api/time-range/objects?start=${start}&end=${end}${iidParam}`);
    try {
      const imgs = await fetch(apiURL(`/api/time-range/images?start=${start}&end=${end}&limit=10${iidParam}`)).then(r => r.json());
      setImages(imgs);
    } catch { setImages([]); }
  };

  const { data: objects, loading } = useApi(query, { deps: [query], enabled: !!query });

  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <h1 className="mb-6 text-2xl font-bold text-gray-900">Time Explorer</h1>

      <div className="mb-6 rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
        <div className="mb-4 flex flex-wrap gap-2">
          {PRESETS.map(p => (
            <button
              key={p.label}
              onClick={() => { setStart(p.start); setEnd(p.end); }}
              className="rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm text-gray-600 transition-colors hover:bg-gray-50"
            >
              {p.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-3">
          <input type="time" value={start} onChange={e => setStart(e.target.value)} className="rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-[#E3002C] focus:outline-none focus:ring-2 focus:ring-[#E3002C]/20" />
          <span className="text-gray-400">to</span>
          <input type="time" value={end} onChange={e => setEnd(e.target.value)} className="rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-[#E3002C] focus:outline-none focus:ring-2 focus:ring-[#E3002C]/20" />
          <button onClick={runQuery} className="rounded-lg bg-[#E3002C] px-5 py-2 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-[#c2001f]">
            Search
          </button>
        </div>
      </div>

      {query && <p className="mb-4 text-sm text-gray-500">{objects?.length || 0} objects found</p>}
      <ObjectGrid objects={objects || []} loading={loading} />

      {images?.length > 0 && (
        <div className="mt-8">
          <h2 className="mb-4 text-lg font-semibold text-gray-900">Sample Frames</h2>
          <div className="flex gap-2 overflow-x-auto pb-2">
            {images.map((url, i) => (
              <img key={i} src={apiURL(url)} alt="" className="h-20 w-28 flex-shrink-0 rounded-lg object-cover" loading="lazy" />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
