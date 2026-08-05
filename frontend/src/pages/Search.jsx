import { useState } from 'react';
import { Link } from 'react-router-dom';
import { getJSON, apiURL } from '../api/client';
import EmptyState from '../components/EmptyState';

export default function Search() {
  const [q, setQ] = useState('');
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const handleSearch = async () => {
    if (!q.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const data = await getJSON(`/api/search?q=${encodeURIComponent(q.trim())}`);
      setResult(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <h1 className="mb-6 text-2xl font-bold text-gray-900">Search</h1>

      <div className="mb-6 flex gap-3">
        <input
          type="text"
          className="flex-1 rounded-lg border border-gray-300 px-4 py-2.5 text-sm placeholder-gray-400 focus:border-[#E3002C] focus:outline-none focus:ring-2 focus:ring-[#E3002C]/20"
          placeholder="Object ID, category name, or frame filename..."
          value={q}
          onChange={e => setQ(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && handleSearch()}
        />
        <button onClick={handleSearch} disabled={loading} className="rounded-lg bg-[#E3002C] px-6 py-2.5 text-sm font-semibold text-white shadow-sm transition-colors hover:bg-[#c2001f] disabled:opacity-50">
          {loading ? '...' : 'Search'}
        </button>
      </div>

      {error && <div className="mb-4 rounded-lg bg-red-50 p-4 text-sm text-red-700">{error}</div>}

      {result && (
        <div className="rounded-xl border border-gray-200 bg-white p-6 shadow-sm">
          {result.type === 'empty' && <EmptyState icon="🔍" title="Enter a search term" />}
          {result.type === 'object' && !result.found && (
            <EmptyState icon="🔍" title={`Object #${result.object_id} not found`} />
          )}
          {result.type === 'object' && result.found && (
            <Link to={`/objects/${result.object_id}`} className="block rounded-lg border border-gray-100 p-4 transition-colors hover:border-[#E3002C]/30 hover:bg-red-50/50">
              <div className="text-lg font-semibold text-[#E3002C]">Object #{result.object_id}</div>
              <div className="mt-1 text-sm text-gray-600">{result.object?.category} — {result.object?.detection_count} detections</div>
            </Link>
          )}
          {result.type === 'category' && (
            <Link to={`/categories/${encodeURIComponent(result.category)}`} className="block rounded-lg border border-gray-100 p-4 transition-colors hover:border-[#E3002C]/30 hover:bg-red-50/50">
              <div className="text-lg font-semibold text-[#E3002C]">Category: {result.category}</div>
              <div className="mt-1 text-sm text-gray-600">{result.objects?.length} objects</div>
            </Link>
          )}
          {result.type === 'frame' && (
            <div>
              <div className="mb-3 font-mono text-sm text-gray-500">Frame: {result.filename}</div>
              <img src={apiURL(`/inspection/images/${result.filename}`)} alt="" className="mb-4 max-h-80 rounded-lg object-contain" />
              <div className="text-sm text-gray-600">{result.objects?.length || 0} object(s) in this frame</div>
              <div className="mt-3 flex flex-wrap gap-2">
                {(result.objects || []).map(o => (
                  <Link key={o.object_id} to={`/objects/${o.object_id}`} className="rounded-lg bg-gray-100 px-3 py-1.5 text-sm text-gray-700 transition-colors hover:bg-[#E3002C]/10 hover:text-[#E3002C]">
                    #{o.object_id} ({o.category})
                  </Link>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
