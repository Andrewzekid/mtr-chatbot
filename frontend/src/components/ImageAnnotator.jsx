import { useEffect, useState } from "react";

const WS_URL = import.meta.env.VITE_WS_URL || "ws://localhost:8000/ws";
const API_ROOT =
  import.meta.env.VITE_API_URL ||
  WS_URL.replace("ws://", "http://").replace("wss://", "https://").replace(/\/ws$/, "");

/**
 * Image anomaly annotator panel.
 *
 * Lets the user upload an image, type a question, and receive the image back
 * with visual annotations drawn by the vision LLM plus a concise description.
 *
 * @param {{ disabled?: boolean }} props
 */
export default function ImageAnnotator({ disabled = false }) {
  const [file, setFile] = useState(null);
  const [previewUrl, setPreviewUrl] = useState(null);
  const [question, setQuestion] = useState("What anomalies are in this image?");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);
  const [selectedImage, setSelectedImage] = useState(null);

  useEffect(() => {
    return () => {
      if (previewUrl) {
        URL.revokeObjectURL(previewUrl);
      }
    };
  }, [previewUrl]);

  const handleFileChange = (event) => {
    const selected = event.target.files?.[0];
    if (!selected) {
      return;
    }
    if (previewUrl) {
      URL.revokeObjectURL(previewUrl);
    }
    setFile(selected);
    setPreviewUrl(URL.createObjectURL(selected));
    setResult(null);
    setError(null);
  };

  const handleSubmit = async (event) => {
    event.preventDefault();
    if (!file || loading || disabled) {
      return;
    }

    setLoading(true);
    setError(null);
    setResult(null);

    const formData = new FormData();
    formData.append("image", file);
    formData.append("question", question);

    try {
      const response = await fetch(`${API_ROOT}/annotate-image`, {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        const body = await response.text();
        throw new Error(`HTTP ${response.status}: ${body}`);
      }

      const data = await response.json();
      setResult(data);
    } catch (err) {
      setError(err.message || "Failed to annotate image");
    } finally {
      setLoading(false);
    }
  };

  const annotatedSrc = result
    ? `data:${result.mime_type || "image/png"};base64,${result.annotated_image_base64}`
    : null;

  return (
    <section className="card annotator-card">
      <h2>Image Anomaly Annotator</h2>
      <p className="annotator-help">
        Upload an inspection image, ask a question, and the vision model will highlight anomalies.
      </p>

      <form className="annotator-form" onSubmit={handleSubmit}>
        <label className="annotator-file-label" htmlFor="annotator-file">
          <span className="annotator-file-button">Choose image</span>
          <span className="annotator-file-name">{file ? file.name : "No file selected"}</span>
          <input
            id="annotator-file"
            type="file"
            accept="image/*"
            onChange={handleFileChange}
            disabled={disabled || loading}
          />
        </label>

        <input
          className="annotator-question"
          type="text"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask about the image..."
          disabled={disabled || loading}
        />

        <button
          className="annotator-submit"
          type="submit"
          disabled={disabled || loading || !file}
        >
          {loading ? "Analyzing..." : "Annotate Image"}
        </button>
      </form>

      {error && <p className="annotator-error">Error: {error}</p>}

      <div className="annotator-result">
        {previewUrl && (
          <div className="annotator-preview">
            <p className="annotator-preview-label">Original preview</p>
            <img src={previewUrl} alt="Selected inspection image" />
          </div>
        )}

        {loading && !result && (
          <div className="annotator-preview">
            <p className="annotator-preview-label">Analyzing...</p>
            <img src={previewUrl} alt="Selected inspection image" />
          </div>
        )}

        {annotatedSrc && (
          <div className="annotator-output">
            <p className="annotator-preview-label">Annotated result</p>
            <button
              type="button"
              className="annotator-image-button"
              onClick={() => setSelectedImage(annotatedSrc)}
            >
              <img src={annotatedSrc} alt="Annotated inspection image" />
            </button>
            {result?.description && (
              <p className="annotator-description">{result.description}</p>
            )}
          </div>
        )}
      </div>

      {selectedImage && (
        <div className="image-lightbox" onClick={() => setSelectedImage(null)}>
          <img src={selectedImage} alt="Full annotated image" />
        </div>
      )}
    </section>
  );
}
