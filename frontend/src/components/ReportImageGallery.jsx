import { useEffect, useState } from "react";

const WS_URL = import.meta.env.VITE_WS_URL || "ws://localhost:8000/ws";
const API_ROOT = import.meta.env.VITE_API_URL || WS_URL.replace("ws://", "http://").replace("wss://", "https://").replace(/\/ws$/, "");

export default function ReportImageGallery() {
  const [images, setImages] = useState([]);
  const [selected, setSelected] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_ROOT}/reports/image-list`)
      .then((res) => {
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        return res.json();
      })
      .then((data) => {
        if (!cancelled) {
          setImages(data.images || []);
        }
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err.message);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (error) {
    return (
      <section className="card report-gallery">
        <h2>Anomaly Images</h2>
        <p className="text-muted">Could not load images: {error}</p>
      </section>
    );
  }

  if (images.length === 0) {
    return null;
  }

  return (
    <section className="card report-gallery">
      <h2>Anomaly Images ({images.length})</h2>
      <div className="image-grid">
        {images.map((src) => (
          <button
            key={src}
            type="button"
            className="image-thumb"
            onClick={() => setSelected(src)}
          >
            <img src={`${API_ROOT}${src}`} alt={`Anomaly ${src}`} loading="lazy" />
          </button>
        ))}
      </div>
      {selected && (
        <div className="image-lightbox" onClick={() => setSelected(null)}>
          <img src={`${API_ROOT}${selected}`} alt="Selected anomaly" />
        </div>
      )}
    </section>
  );
}
