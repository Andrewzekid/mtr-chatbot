import { Link } from 'react-router-dom';
import { apiURL } from '../api/client';

const CAT_ICONS = {
  'Lights': '💡',
  'Advertisement Board': '📺',
  'Ticket Gate': '🚇',
  'Map': '🗺️',
  'TV': '📺',
  'Exit Sign': '🚷',
};

export default function ObjectCard({ obj }) {
  const imgUrl = obj.frame_image_url || (obj.frame_filename ? `/inspection/images/${obj.frame_filename}` : null);

  return (
    <Link
      to={`/objects/${obj.id}`}
      className="group overflow-hidden rounded-xl border border-gray-200 bg-white shadow-sm transition-all hover:shadow-md hover:border-[#E3002C]/30 hover:-translate-y-0.5"
    >
      {imgUrl ? (
        <img
          className="h-36 w-full object-cover transition-transform group-hover:scale-105"
          src={apiURL(imgUrl)}
          alt={obj.category}
          loading="lazy"
        />
      ) : (
        <div className="flex h-36 w-full items-center justify-center bg-gray-50 text-4xl">
          {CAT_ICONS[obj.category] || '📦'}
        </div>
      )}
      <div className="p-3">
        <div className="flex items-center justify-between">
          <span className="text-sm font-semibold text-gray-800">{obj.category} #{obj.id}</span>
          <span className="text-xs text-gray-400">{obj.detection_count} det</span>
        </div>
      </div>
    </Link>
  );
}
