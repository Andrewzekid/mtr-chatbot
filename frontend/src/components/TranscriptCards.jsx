import { useState } from "react";
import MarkdownImageText from "./MarkdownImageText";

const EMOTION_EMOJI = {
  happy: "😊",
  sad: "😢",
  angry: "😠",
  fear: "😨",
  surprised: "😮",
  disgust: "🤢",
  neutral: "😐",
};
const FALLBACK_EMOJI = "🙂";

/**
 * Shows the live transcript ("You") and streaming assistant response cards.
 *
 * @param {{
 *   transcript: string,
 *   transcriptRaw: string,
 *   assistantText: string,
 *   userEmotion: string,
 * }} props
 */
export default function TranscriptCards({ transcript, transcriptRaw, assistantText, userEmotion, onSendText, textInputDisabled }) {
  const [selectedImage, setSelectedImage] = useState(null);
  const [draft, setDraft] = useState("");

  const submitText = (event) => {
    event.preventDefault();
    const text = draft.trim();
    if (!text || textInputDisabled || !onSendText) {
      return;
    }
    onSendText(text);
    setDraft("");
  };

  return (
    <>
      <section className="card">
        <h2>
          You
          <span className="emotion-emoji" title={`Detected emotion: ${userEmotion || "unknown"}`}>
            {EMOTION_EMOJI[userEmotion] || FALLBACK_EMOJI}
          </span>
        </h2>
        <p>{transcript || "Your transcript appears here."}</p>
        {transcriptRaw ? <p className="sense-raw">SenseVoice raw: {transcriptRaw}</p> : null}
        <form className="text-input-row" onSubmit={submitText}>
          <input
            type="text"
            className="text-input"
            placeholder="Type a message…"
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            disabled={textInputDisabled}
            aria-label="Type a message"
          />
          <button type="submit" className="text-send-btn" disabled={textInputDisabled || !draft.trim()}>
            Send
          </button>
        </form>
      </section>

      <section className="card accent">
        <h2>Assistant</h2>
        <MarkdownImageText
          text={assistantText}
          className="assistant-streaming-text"
          emptyText="Streaming response will appear here."
          onImageClick={setSelectedImage}
        />
      </section>

      {selectedImage && (
        <div className="image-lightbox" onClick={() => setSelectedImage(null)}>
          <img src={selectedImage} alt="Selected assistant frame" />
        </div>
      )}
    </>
  );
}
