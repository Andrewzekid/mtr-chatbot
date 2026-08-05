import ObjectCard from './ObjectCard';
import EmptyState from './EmptyState';

export default function ObjectGrid({ objects, loading }) {
  if (loading) return <div className="flex items-center justify-center py-12 text-gray-400">Loading...</div>;
  if (!objects?.length) return <EmptyState icon="📭" title="No objects found" />;

  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
      {objects.map(obj => (
        <ObjectCard key={obj.id} obj={obj} />
      ))}
    </div>
  );
}
