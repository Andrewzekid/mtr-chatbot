import { useState } from 'react';
import { useApi } from '../hooks/useApi';
import { useConsoleState } from '../state/ConsoleState';
import AnomalyList from '../components/AnomalyList';
import ExportButton from '../components/ExportButton';

export default function Anomalies() {
  const { inspectionId } = useConsoleState();
  const iidParam = inspectionId ? `&inspection_id=${inspectionId}` : '';
  const [filterType, setFilterType] = useState('');

  const { data: types } = useApi('/api/anomalies/types');
  const { data: summary } = useApi(`/api/anomalies/summary${iidParam}`);
  const typeParam = filterType ? `&anomaly_type=${encodeURIComponent(filterType)}` : '';
  const { data: anomalies, loading } = useApi(`/api/anomalies?limit=all${iidParam}${typeParam}`);

  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <div className="mb-6 flex items-center justify-between">
        <h1 className="text-2xl font-bold text-gray-900">Anomalies</h1>
        <ExportButton query="anomalies" args={{ anomaly_type: filterType || undefined }} />
      </div>

      {summary?.by_type && (
        <div className="mb-6 flex flex-wrap items-center gap-2">
          {summary.by_type.map(t => (
            <button
              key={t.type}
              onClick={() => setFilterType(filterType === t.type ? '' : t.type)}
              className={`rounded-full px-4 py-1.5 text-sm font-medium transition-colors ${
                filterType === t.type
                  ? 'bg-[#E3002C] text-white'
                  : 'bg-white text-gray-600 hover:bg-gray-50 border border-gray-200'
              }`}
            >
              {t.type} ({t.count})
            </button>
          ))}
          <span className="ml-2 text-sm text-gray-400">{summary.total_abnormalities} total</span>
        </div>
      )}

      <AnomalyList anomalies={anomalies || []} loading={loading} />
    </div>
  );
}
