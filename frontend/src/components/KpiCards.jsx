import { useApi } from '../hooks/useApi';
import { useConsoleState } from '../state/ConsoleState';

export default function KpiCards() {
  const { db } = useConsoleState();
  const { data: anomalySummary } = useApi('/api/anomalies/summary');

  const cards = [
    { label: 'Total Objects', value: db?.total_objects ?? 0, color: 'text-[#E3002C]', bg: 'bg-red-50' },
    { label: 'Categories', value: db?.categories?.length ?? 0, color: 'text-green-600', bg: 'bg-green-50' },
    { label: 'Inspections', value: db?.inspections?.length ?? 0, color: 'text-blue-600', bg: 'bg-blue-50' },
    { label: 'Anomalies', value: anomalySummary?.total_abnormalities ?? 0, color: 'text-amber-600', bg: 'bg-amber-50' },
  ];

  return (
    <div className="mb-8 grid grid-cols-2 gap-4 lg:grid-cols-4">
      {cards.map(c => (
        <div key={c.label} className={`${c.bg} rounded-xl border border-gray-100 p-5 shadow-sm`}>
          <div className={`text-3xl font-bold ${c.color}`}>{c.value}</div>
          <div className="mt-1 text-sm font-medium text-gray-500">{c.label}</div>
        </div>
      ))}
    </div>
  );
}