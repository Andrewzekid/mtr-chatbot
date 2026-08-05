import { Link } from 'react-router-dom';
import { useConsoleState } from '../state/ConsoleState';

const CAT_ICONS = {
  'Lights': '💡',
  'Advertisement Board': '📺',
  'Ticket Gate': '🚇',
  'Map': '🗺️',
  'TV': '📺',
  'Exit Sign': '🚷',
};

const CAT_COLORS = {
  'Lights': 'from-yellow-50 to-yellow-100 border-yellow-200 hover:border-yellow-400',
  'Advertisement Board': 'from-blue-50 to-blue-100 border-blue-200 hover:border-blue-400',
  'Ticket Gate': 'from-purple-50 to-purple-100 border-purple-200 hover:border-purple-400',
  'Map': 'from-green-50 to-green-100 border-green-200 hover:border-green-400',
  'TV': 'from-indigo-50 to-indigo-100 border-indigo-200 hover:border-indigo-400',
  'Exit Sign': 'from-red-50 to-red-100 border-red-200 hover:border-red-400',
};

export default function CategoryTiles() {
  const { db, inspectionId } = useConsoleState();
  const counts = db?.category_counts || [];

  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-6">
      {counts.map(c => {
        const colorClass = CAT_COLORS[c.category] || 'from-gray-50 to-gray-100 border-gray-200 hover:border-gray-400';
        return (
          <Link
            key={c.category}
            to={`/categories/${encodeURIComponent(c.category)}`}
            className={`group rounded-xl border bg-gradient-to-br p-5 text-center shadow-sm transition-all hover:shadow-md hover:-translate-y-0.5 ${colorClass}`}
          >
            <div className="text-3xl mb-2">{CAT_ICONS[c.category] || '📦'}</div>
            <div className="text-sm font-semibold text-gray-800">{c.category}</div>
            <div className="mt-1 text-xs text-gray-500">{c.count ?? c.object_count ?? 0} objects</div>
          </Link>
        );
      })}
    </div>
  );
}
