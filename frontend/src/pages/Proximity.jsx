import { useState } from 'react';
import { useApi } from '../hooks/useApi';
import { useConsoleState } from '../state/ConsoleState';

const ALL_CATS = ['Lights', 'Advertisement Board', 'Ticket Gate', 'Map', 'TV', 'Exit Sign'];

export default function Proximity() {
  const { inspectionId } = useConsoleState();
  const [target, setTarget] = useState('Lights');
  const [others, setOthers] = useState(['Ticket Gate']);
  const [radius, setRadius] = useState(2.0);
  const [submitted, setSubmitted] = useState(null);

  const iidParam = inspectionId ? `&inspection_id=${inspectionId}` : '';
  const enabled = submitted !== null;
  const path = enabled
    ? `/api/proximity?target=${encodeURIComponent(submitted.target)}&others=${submitted.others.map(encodeURIComponent).join(',')}&radius_m=${submitted.radius}&with_images=true${iidParam}`
    : null;
  const { data, loading } = useApi(path, { deps: [path], enabled });

  const toggleOther = (cat) => {
    setOthers(prev => prev.includes(cat) ? prev.filter(c => c !== cat) : [...prev, cat]);
  };

  const results = data?.results || [];

  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <h1 className="mb-6 text-2xl font-bold text-gray-900">Proximity Explorer</h1>

      <div className="mb-6 rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
        <div className="space-y-4">
          <div className="flex items-center gap-4">
            <label className="w-32 text-sm font-medium text-gray-700">Target:</label>
            <select value={target} onChange={e => setTarget(e.target.value)} className="rounded-lg border border-gray-300 px-3 py-2 text-sm focus:border-[#E3002C] focus:outline-none focus:ring-2 focus:ring-[#E3002C]/20">
              {ALL_CATS.map(c => <option key={c} value={c}>{c}</option>)}
            </select>
          </div>
          <div className="flex items-start gap-4">
            <label className="w-32 pt-2 text-sm font-medium text-gray-700">Nearby:</label>
            <div className="flex flex-wrap gap-2">
              {ALL_CATS.map(c => (
                <button
                  key={c}
                  onClick={() => toggleOther(c)}
                  className={`rounded-full px-3 py-1 text-sm font-medium transition-colors ${
                    others.includes(c)
                      ? 'bg-[#E3002C] text-white'
                      : 'bg-gray-100 text-gray-600 hover:bg-gray-200'
                  }`}
                >
                  {c}
                </button>
              ))}
            </div>
          </div>
          <div className="flex items-center gap-4">
            <label className="w-32 text-sm font-medium text-gray-700">Radius: {radius}m</label>
            <input type="range" min={0.5} max={10} step={0.5} value={radius} onChange={e => setRadius(parseFloat(e.target.value))} className="flex-1" />
          </div>
          <button onClick={() => setSubmitted({ target, others, radius })} className="rounded-lg bg-[#E3002C] px-6 py-2.5 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-[#c2001f]">
            Find nearby
          </button>
        </div>
      </div>

      {enabled && loading && <div className="py-8 text-center text-gray-400">Searching...</div>}
      {enabled && !loading && (
        <div className="space-y-4">
          <p className="text-sm text-gray-500">{results.length} result(s) found</p>
          {results.map(r => (
            <div key={r.object_id} className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
              <div className="mb-3 text-sm font-semibold text-gray-800">
                Object #{r.object_id} at [{r.centroid_x?.toFixed(1)}, {r.centroid_y?.toFixed(1)}, {r.centroid_z?.toFixed(1)}]
              </div>
              <div className="flex flex-wrap gap-2">
                {(r.nearby || []).map(n => (
                  <div key={n.object_id} className="flex items-center gap-2 rounded-lg border border-gray-100 bg-gray-50 px-3 py-2">
                    <span className="text-sm font-medium text-gray-700">{n.category}</span>
                    <span className="rounded-full bg-[#E3002C]/10 px-2 py-0.5 text-xs font-semibold text-[#E3002C]">{n.distance_m?.toFixed(2)}m</span>
                    <span className="text-xs text-gray-400">#{n.object_id}</span>
                    {n.sample_image_path_url && <img src={n.sample_image_path_url} alt="" className="h-8 w-10 rounded object-cover" loading="lazy" />}
                  </div>
                ))}
                {!r.nearby?.length && <span className="text-sm text-gray-400">Nothing nearby</span>}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
