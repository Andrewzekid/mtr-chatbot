import { useState } from "react";

const WS_URL = import.meta.env.VITE_WS_URL || "ws://localhost:8000/ws";
const API_ROOT =
  import.meta.env.VITE_API_URL ||
  WS_URL.replace("ws://", "http://").replace("wss://", "https://").replace(/\/ws$/, "");

const IMAGE_RE = /!\[([^\]]*)\]\(([^)]+)\)/g;

/**
 * Render text that may contain markdown image links as inline thumbnails.
 *
 * @param {string} text - Source text.
 * @param {(src: string) => void} onImageClick - Called when a thumbnail is clicked.
 * @returns {Array<JSX.Element>}
 */
export function renderTextWithImages(text, onImageClick) {
  const parts = [];
  let match;
  let lastIndex = 0;

  // eslint-disable-next-line no-cond-assign
  while ((match = IMAGE_RE.exec(text)) !== null) {
    const [full, alt, src] = match;
    if (match.index > lastIndex) {
      parts.push(
        <span key={`text-${match.index}`}>{text.slice(lastIndex, match.index)}</span>
      );
    }
    const resolvedSrc = src.startsWith("http") ? src : `${API_ROOT}${src}`;
    parts.push(
      <button
        key={`img-${match.index}`}
        type="button"
        className="chat-image-thumb"
        onClick={() => onImageClick(resolvedSrc)}
        title={alt || "Inspection frame"}
      >
        <img src={resolvedSrc} alt={alt || "Inspection frame"} loading="lazy" />
      </button>
    );
    lastIndex = match.index + full.length;
  }

  if (lastIndex < text.length) {
    parts.push(<span key="text-tail">{text.slice(lastIndex)}</span>);
  }

  return parts;
}

/**
 * Markdown-aware text renderer.
 *
 * By default it manages its own image lightbox. If you want a parent component
 * to control the lightbox, pass `onImageClick`.
 *
 * @param {{
 *   text: string,
 *   className?: string,
 *   emptyText?: string,
 *   onImageClick?: (src: string) => void,
 * }} props
 */
export default function MarkdownImageText({
  text,
  className = "",
  emptyText = "",
  onImageClick,
}) {
  const [internalSelected, setInternalSelected] = useState(null);

  const handleImageClick = onImageClick || setInternalSelected;
  const selectedImage = onImageClick ? null : internalSelected;

  return (
    <>
      {text ? (
        <div className={`markdown-image-text ${className}`.trim()}>
          {renderTextWithImages(text, handleImageClick)}
        </div>
      ) : (
        <p className={className}>{emptyText}</p>
      )}
      {selectedImage && (
        <div className="image-lightbox" onClick={() => setInternalSelected(null)}>
          <img src={selectedImage} alt="Selected inspection frame" />
        </div>
      )}
    </>
  );
}
