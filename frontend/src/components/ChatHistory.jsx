import { useState } from "react";
import MarkdownImageText from "./MarkdownImageText";

/**
 * Renders the full conversation history, including inline object images
 * returned by the assistant as markdown image links.
 *
 * @param {{ chatHistory: Array<{ id: string, userText: string, assistantText: string, interrupted: boolean }> }} props
 */
export default function ChatHistory({ chatHistory }) {
  const [selectedImage, setSelectedImage] = useState(null);

  return (
    <section className="card history">
      <h2>Chat History</h2>
      {chatHistory.length === 0 ? (
        <p>No previous turns yet.</p>
      ) : (
        <div className="history-list">
          {chatHistory.map((turn, idx) => (
            <article key={turn.id || idx} className="history-turn">
              <p className="history-you">You: {turn.userText || "..."}</p>
              <div className="history-assistant">
                <span className="history-assistant-label">Assistant: </span>
                {turn.assistantText ? (
                  <MarkdownImageText
                    text={turn.assistantText}
                    onImageClick={setSelectedImage}
                  />
                ) : (
                  <span>{turn.interrupted ? "[interrupted]" : "..."}</span>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
      {selectedImage && (
        <div className="image-lightbox" onClick={() => setSelectedImage(null)}>
          <img src={selectedImage} alt="Selected inspection frame" />
        </div>
      )}
    </section>
  );
}
