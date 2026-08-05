import { Link, useParams } from 'react-router-dom';
import { useApi } from '../hooks/useApi';
import CategoryTiles from '../components/CategoryTiles';

export default function InspectionDetail() {
  const { id } = useParams();
  const iidParam = `?inspection_id=${id}`;

  const { data: summary } = useApi(`/api/summary${iidParam}`, { deps: [id] });
  const { data: recent } = useApi(`/api/recent-objects?limit=10${iidParam}`, { deps: [id] });
  const { data: clusters } = useApi(`/api/temporal-clusters?window_ms=500&top_n=8${iidParam}`, { deps: [id] });
  const { data: anomalies } = useApi(`/api/anomalies?limit=all${iidParam}`, { deps: [id] });
  const { data: topObjects } = useApi(`/api/detection-counts?top_n=10${iidParam}`, { deps: [id] });

  const insLabel = id === '1' || summary?.inspection_id === parseInt(id) ? `Inspection #${id}` : `Inspection #${id}`;

  return (
    <div className="mx-auto max-w-7xl px-6 py-8">
      <div className="mb-6 flex items-center justify-between">
        <div>
          <Link to="/overview" className="mb-1 inline-flex items-center gap-1 text-sm text-gray-500 hover:text-gray-700">← Overview</Link>
          <h1 className="text-2xl font-bold text-gray-900">{insLabel}</h1>
          <p className="mt-1 text-sm text-gray-500">{summary?.total_objects || 0} objects · {anomalies?.length || 0} anomalies</p>
        </div>
        <Link to="/defects" className="rounded-lg border border-gray-200 bg-white px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50">
          View Anomaly Report →
        </Link>
      </div>

      {/* KPI cards for this inspection */}
      <div className="mb-8 grid grid-cols-2 gap-4 md:grid-cols-4">
        <div className="rounded-xl border border-gray-200 bg-red-50 p-5 shadow-sm">
          <div className="text-3xl font-bold text-[#E3002C]">{summary?.total_objects ?? 0}</div>
          <div className="mt-1 text-sm text-gray-500">Objects Detected</div>
        </div>
        <div className="rounded-xl border border-gray-200 bg-blue-50 p-5 shadow-sm">
          <div className="text-3xl font-bold text-blue-600">{summary?.categories?.length ?? 0}</div>
          <div className="mt-1 text-sm text-gray-500">Categories</div>
        </div>
        <div className="rounded-xl border border-gray-200 bg-amber-50 p-5 shadow-sm">
          <div className="text-3xl font-bold text-amber-600">{anomalies?.length ?? 0}</div>
          <div className="mt-1 text-sm text-gray-500">Anomalies Found</div>
        </div>
        <div className="rounded-xl border border-gray-200 bg-green-50 p-5 shadow-sm">
          <div className="text-3xl font-bold text-green-600">{(anomalies?.length ?? 0) === 0 ? 'PASS' : 'REVIEW'}</div>
          <div className="mt-1 text-sm text-gray-500">Status</div>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        <div className="lg:col-span-2 space-y-6">
          {/* Category breakdown */}
          <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
            <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-gray-700">Category Breakdown</h2>
            <CategoryTiles />
          </div>

          {/* Top detected objects */}
          {topObjects?.length > 0 && (
            <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
              <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-gray-700">Most Detected Objects</h2>
              <div className="space-y-2">
                {topObjects.map((o, i) => (
                  <Link
                    key={o.id}
                    to={`/objects/${o.id}`}
                    className="flex items-center gap-3 rounded-lg border border-gray-100 p-3 transition-colors hover:border-[#E3002C]/30 hover:bg-red-50/50"
                  >
                    <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-gray-100 text-xs font-bold text-gray-600">
                      {i + 1}
                    </div>
                    <div className="flex-1">
                      <span className="text-sm font-medium text-gray-900">{o.category}</span>
                      <span className="ml-2 font-mono text-xs text-gray-400">#{o.id}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <div className="h-2 w-24 rounded-full bg-gray-100">
                        <div className="h-2 rounded-full bg-[#E3002C]" style={{ width: `${Math.min(100, (o.detection_count / topObjects[0].detection_count) * 100)}%` }} />
                      </div>
                      <span className="w-10 text-right text-xs font-medium text-gray-700">{o.detection_count}</span>
                    </div>
                  </Link>
                ))}
              </div>
            </div>
          )}

          {/* Recent Objects */}
          <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
            <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-gray-700">Recent Detections</h2>
            <div className="space-y-2">
              {(recent || []).map(obj => (
                <Link
                  key={obj.id}
                  to={`/objects/${obj.id}`}
                  className="flex items-center justify-between rounded-lg border border-gray-100 p-3 transition-colors hover:border-[#E3002C]/30 hover:bg-red-50/50"
                >
                  <div className="flex items-center gap-3">
                    <span className="font-mono text-xs text-gray-400">#{obj.id}</span>
                    <span className="text-sm font-medium text-gray-800">{obj.category}</span>
                  </div>
                  <div className="flex items-center gap-3 text-xs text-gray-400">
                    <span>{obj.detection_count} dets</span>
                    <span>{obj.last_seen_ns_iso?.slice(11, 19)}</span>
                  </div>
                </Link>
              ))}
              {!recent?.length && <p className="py-4 text-center text-sm text-gray-400">No objects</p>}
            </div>
          </div>
        </div>

        {/* Right column */}
        <div className="space-y-6">
          {/* Anomalies for this inspection */}
          {anomalies?.length > 0 && (
            <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
              <div className="mb-4 flex items-center justify-between">
                <h2 className="text-sm font-semibold uppercase tracking-wider text-gray-700">Anomalies</h2>
                <Link to="/defects" className="text-xs font-medium text-[#E3002C] hover:underline">View report →</Link>
              </div>
              <div className="space-y-2">
                {anomalies.map(a => (
                  <Link
                    key={a.id}
                    to={`/defects?focus=${a.id}`}
                    className="block rounded-lg border-l-4 border-amber-400 bg-amber-50/50 p-3 transition-colors hover:bg-amber-50"
                  >
                    <div className="text-sm font-medium text-gray-900">{a.object || 'Unknown'}</div>
                    <div className="mt-0.5 text-xs text-gray-500">{a.type}</div>
                  </Link>
                ))}
              </div>
            </div>
          )}

          {/* Busiest Moments */}
          {clusters?.length > 0 && (
            <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
              <h2 className="mb-4 text-sm font-semibold uppercase tracking-wider text-gray-700">Detection Timeline</h2>
              <div className="space-y-2">
                {clusters.map((c, i) => {
                  const cats = Object.entries(c.categories || {}).sort((a, b) => b[1] - a[1]);
                  return (
                    <div key={i} className="flex items-center gap-3 rounded-lg border border-gray-100 p-3">
                      <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-[#E3002C]/10 text-sm font-bold text-[#E3002C]">
                        {c.detection_count}
                      </div>
                      <div className="flex-1">
                        <div className="text-sm font-medium text-gray-800">{c.start_ns_iso?.slice(11, 19)}</div>
                        <div className="text-xs text-gray-500">{cats.map(([k, v]) => `${v}× ${k}`).join(', ')}</div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}