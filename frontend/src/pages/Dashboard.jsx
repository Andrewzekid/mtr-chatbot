import { Link } from 'react-router-dom';
import { useApi } from '../hooks/useApi';
import { useConsoleState } from '../state/ConsoleState';
import KpiCards from '../components/KpiCards';
import CategoryTiles from '../components/CategoryTiles';

export default function Dashboard() {
  const { inspectionId } = useConsoleState();
  const iidParam = inspectionId ? `?inspection_id=${inspectionId}` : '';
  const { data: recent } = useApi(`/api/recent-objects?limit=8${iidParam}`);
  const { data: clusters } = useApi(`/api/temporal-clusters?window_ms=500&top_n=5${iidParam}`);

  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <h1 className="mb-6 text-2xl font-bold text-gray-900">Inspection Dashboard</h1>
      <KpiCards />

      <h2 className="mb-4 text-lg font-semibold text-gray-900">Categories</h2>
      <CategoryTiles />

      <div className="mt-8 grid grid-cols-1 gap-6 lg:grid-cols-2">
        {/* Recent Objects */}
        <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
          <h3 className="mb-4 text-sm font-semibold text-gray-900 uppercase tracking-wider">Recent Objects</h3>
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
                <span className="text-xs text-gray-400">{obj.last_seen_ns_iso?.slice(11, 19)}</span>
              </Link>
            ))}
            {!recent?.length && <p className="py-4 text-center text-sm text-gray-400">No recent objects</p>}
          </div>
        </div>

        {/* Busiest Moments */}
        <div className="rounded-xl border border-gray-200 bg-white p-5 shadow-sm">
          <h3 className="mb-4 text-sm font-semibold text-gray-900 uppercase tracking-wider">Busiest Moments</h3>
          <div className="space-y-2">
            {(clusters || []).map((c, i) => {
              const cats = Object.entries(c.categories || {}).sort((a, b) => b[1] - a[1]);
              return (
                <div key={i} className="flex items-center gap-3 rounded-lg border border-gray-100 p-3">
                  <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-[#E3002C]/10 text-sm font-bold text-[#E3002C]">
                    {c.detection_count}
                  </div>
                  <div className="flex-1">
                    <div className="text-sm font-medium text-gray-800">
                      {c.start_ns_iso?.slice(11, 19)}
                    </div>
                    <div className="text-xs text-gray-500">
                      {cats.map(([k, v]) => `${v}× ${k}`).join(', ')}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}
