import { apiURL } from '../api/client';
import EmptyState from './EmptyState';

export default function AnomalyList({ anomalies, loading }) {
  if (loading) return <div className="flex items-center justify-center py-12 text-gray-400">Loading...</div>;
  if (!anomalies?.length) return <EmptyState icon="✅" title="No anomalies found" subtitle="All objects match ground truth" />;

  return (
    <div className="space-y-3">
      {anomalies.map((a, i) => {
        const type = a.type || a.anomaly_type || 'Unknown';
        const desc = a.note || a.description;
        const gtUrl = a.gt_filename_url || (a.gt_filename ? `/inspection/images/${a.gt_filename}` : null);
        const inspUrl = a.inspection_filename_url || (a.inspection_filename ? `/inspection/images/${a.inspection_filename}` : null);
        return (
          <div key={i} className="flex items-center gap-4 rounded-xl border border-gray-200 bg-white p-4 shadow-sm transition-colors hover:border-amber-300">
          {gtUrl && (
            <img src={apiURL(gtUrl)} alt="Ground truth" className="h-20 w-20 rounded-lg object-cover" loading="lazy" />
          )}

          <div className="flex h-20 w-20 items-center justify-center rounded-lg bg-gray-100 text-sm font-bold text-gray-400">
            GT vs Inspection
          </div>

          {inspUrl && (
            <img src={apiURL(inspUrl)} alt="Inspection" className="h-20 w-20 rounded-lg object-cover" loading="lazy" />
          )}

          <div className="flex-1">
            <div className="text-sm font-medium text-gray-800">{desc || type}</div>
            {desc && <div className="mt-1 text-xs text-gray-500">{type}</div>}
          </div>

          <span className="rounded-full bg-amber-50 px-3 py-1 text-xs font-medium text-amber-700 ring-1 ring-inset ring-amber-600/20">
            {type}
          </span>
          </div>
        );
      })}
    </div>
  );
}
