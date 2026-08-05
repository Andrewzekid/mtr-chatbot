import { useState } from 'react';
import { apiURL } from '../api/client';

export default function FrameStrip({ urls, title = '' }) {
  const [lightbox, setLightbox] = useState(null);

  if (!urls?.length) return <div className="py-4 text-center text-sm text-gray-400">No frames available</div>;

  return (
    <>
      {title && <div className="mb-2 text-xs font-medium text-gray-500">{title}</div>}
      <div className="flex gap-2 overflow-x-auto pb-2 scrollbar-thin">
        {urls.map((url, i) => (
          <img
            key={i}
            src={apiURL(url)}
            alt={`Frame ${i + 1}`}
            className="h-16 w-24 flex-shrink-0 cursor-pointer rounded-lg border-2 border-transparent object-cover transition-all hover:border-[#E3002C] hover:shadow-md"
            loading="lazy"
            onClick={() => setLightbox(apiURL(url))}
          />
        ))}
      </div>
      {lightbox && (
        <div
          className="fixed inset-0 z-[200] flex cursor-zoom-out items-center justify-center bg-black/88"
          onClick={() => setLightbox(null)}
        >
          <img src={lightbox} alt="Full size" className="max-h-[92vh] max-w-[92vw] rounded-lg shadow-2xl" />
        </div>
      )}
    </>
  );
}
