import { useState } from 'react';
import { useParams } from 'react-router-dom';
import { useApi } from '../hooks/useApi';
import { useConsoleState } from '../state/ConsoleState';
import ObjectGrid from '../components/ObjectGrid';
import FrameStrip from '../components/FrameStrip';
import ExportButton from '../components/ExportButton';

const VIEWS = ['grid', 'coords', 'gallery', 'timeline'];

export default function CategoryDetail() {
  const { name } = useParams();
  const decoded = decodeURIComponent(name);
  const { inspectionId } = useConsoleState();
  const iidParam = inspectionId ? `&inspection_id=${inspectionId}` : '';
  const [view, setView] = useState('grid');

  const { data: objects, loading } = useApi(
    `/api/category/${encodeURIComponent(decoded)}/objects?limit=all${iidParam}`,
    { deps: [decoded, inspectionId] }
  );
  const { data: coords } = useApi(
    `/api/category/${encodeURIComponent(decoded)}/coordinates${iidParam}`,
    { deps: [decoded, inspectionId] }
  );
  const { data: images } = useApi(
    `/api/category/${encodeURIComponent(decoded)}/images?limit=all${iidParam}`,
    { deps: [decoded, inspectionId] }
  );
  const { data: timeline } = useApi(
    `/api/category/${encodeURIComponent(decoded)}/timeline?bucket_seconds=60${iidParam}`,
    { deps: [decoded, inspectionId] }
  );

  const maxCount = Math.max(...(timeline || []).map(t => t.count || 0), 1);

  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-gray-900">{decoded}</h1>
          <p className="mt-1 text-sm text-gray-500">{objects?.length || 0} objects</p>
        </div>
        <ExportButton query="category_objects" args={{ category: decoded }} />
      </div>

      {/* View tabs */}
      <div className="mb-6 flex gap-2">
        {VIEWS.map(v => (
          <button
            key={v}
            onClick={() => setView(v)}
            className={`rounded-lg px-4 py-2 text-sm font-medium transition-colors ${
              view === v
                ? 'bg-[#E3002C] text-white shadow-sm'
                : 'bg-white text-gray-600 hover:bg-gray-50 border border-gray-200'
            }`}
          >
            {v.charAt(0).toUpperCase() + v.slice(1)}
          </button>
        ))}
      </div>

      {view === 'grid' && <ObjectGrid objects={objects || []} loading={loading} />}

      {view === 'coords' && (
        <div className="overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-gray-200 bg-gray-50">
              <tr>
                <th className="px-4 py-3 font-medium text-gray-500">ID</th>
                <th className="px-4 py-3 font-medium text-gray-500">X</th>
                <th className="px-4 py-3 font-medium text-gray-500">Y</th>
                <th className="px-4 py-3 font-medium text-gray-500">Z</th>
                <th className="px-4 py-3 font-medium text-gray-500">Detections</th>
                <th className="px-4 py-3 font-medium text-gray-500">First seen</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {(coords || []).map(c => (
                <tr key={c.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 font-mono text-gray-900">#{c.id}</td>
                  <td className="px-4 py-3 font-mono text-gray-700">{c.centroid_x?.toFixed(2)}</td>
                  <td className="px-4 py-3 font-mono text-gray-700">{c.centroid_y?.toFixed(2)}</td>
                  <td className="px-4 py-3 font-mono text-gray-700">{c.centroid_z?.toFixed(2)}</td>
                  <td className="px-4 py-3 text-gray-700">{c.detection_count}</td>
                  <td className="px-4 py-3 font-mono text-gray-500">{c.first_seen_ns_iso?.slice(11, 19)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {view === 'gallery' && (
        <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
          <FrameStrip urls={images || []} title={`${decoded} frames`} />
        </div>
      )}

      {view === 'timeline' && (
        <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
          <div className="space-y-2">
            {(timeline || []).map((t, i) => (
              <div key={i} className="flex items-center gap-3">
                <span className="w-12 font-mono text-xs text-gray-500">{t.bucket_ns_iso?.slice(11, 16) || '—'}</span>
                <div className="flex-1">
                  <div className="h-5 rounded bg-gray-100">
                    <div
                      className="h-5 rounded bg-[#E3002C] transition-all duration-300"
                      style={{ width: `${(t.count / maxCount) * 100}%` }}
                    />
                  </div>
                </div>
                <span className="w-8 text-right text-xs font-medium text-gray-700">{t.count}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
