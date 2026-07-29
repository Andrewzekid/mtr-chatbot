import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useWebSocket } from "./hooks/useWebSocket";
import { useAudioPlayback } from "./hooks/useAudioPlayback";
import { useRecorder } from "./hooks/useRecorder";
import StatusPanel from "./components/StatusPanel";
import ChatHistory from "./components/ChatHistory";
import TranscriptCards from "./components/TranscriptCards";
import ReportImageGallery from "./components/ReportImageGallery";
import DebugPanel from "./components/DebugPanel";
import ImageAnnotator from "./components/ImageAnnotator";

const WS_URL = import.meta.env.VITE_WS_URL || "ws://localhost:8000/ws";
const STATUS_URL = WS_URL.replace("ws://", "http://").replace("wss://", "https://").replace(/\/ws$/, "/status");
const ENABLE_BROWSER_TTS_FALLBACK = true;

const INITIAL_RUNTIME = {
  configured_model: "unknown",
  models_loaded: false,
  llm_reachable: false,
  llm_running: false,
  tts_available: false,
  tts_default_voice_id: "unknown",
  tts_chinese_voice_id: "unknown",
  tts_cantonese_voice_id: "unknown",
  tts_chinese_fallback_voice_id: "unknown",
  tts_last_voice_id: "unknown",
  tts_last_voice_reason: "unknown",
  tts_last_text_language: "unknown",
  vram_used_mb: null,
  vram_total_mb: null,
  vram_percent: null,
  gpu_available: false,
};

export default function App() {
  const [transcript, setTranscript] = useState("");
  const [transcriptRaw, setTranscriptRaw] = useState("");
  const [assistantText, setAssistantText] = useState("");
  const [userEmotion, setUserEmotion] = useState("neutral");
  const [chatHistory, setChatHistory] = useState([]);
  const [status, setStatus] = useState("Hold Space to talk");
  const [runtime, setRuntime] = useState(INITIAL_RUNTIME);
  const [toolCalls, setToolCalls] = useState([]);
  const [toolRouterRaw, setToolRouterRaw] = useState(null);

  // Coordination refs shared across callbacks
  const isSpacePressedRef = useRef(false);
  const isAssistantStreamingRef = useRef(false);
  const currentRequestIdRef = useRef(null);
  const interruptedRequestIdsRef = useRef(new Set());
  const hasPlayedAudioRef = useRef(false);
  const pendingSpeechRef = useRef("");
  const pendingSpeechLanguageRef = useRef("english");

  const { ttsStreamActive, setTtsStreamActive, ensureAudioContext, enqueueChunk, stopAll: stopAllAudio, hasPendingPlayback } =
    useAudioPlayback();

  // ── Browser TTS fallback ──────────────────────────────────────────────────

  function speakFallback(text, ttsLanguage = "english") {
    const synth = window.speechSynthesis;
    if (!synth || !text?.trim()) {
      return;
    }
    const utterance = new SpeechSynthesisUtterance(text);
    utterance.rate = 1;
    utterance.pitch = 1;
    let preferredLang = "en-US";
    if (ttsLanguage === "cantonese") {
      preferredLang = "zh-HK";
    } else if (ttsLanguage === "chinese") {
      preferredLang = "zh-CN";
    }
    utterance.lang = preferredLang;
    const voices = synth.getVoices();
    const matchedVoice = voices.find((v) => v.lang?.toLowerCase().startsWith(preferredLang.toLowerCase()));
    if (matchedVoice) {
      utterance.voice = matchedVoice;
    }
    synth.speak(utterance);
  }

  function streamFallbackSpeech(forceFlush) {
    if (!ENABLE_BROWSER_TTS_FALLBACK) {
      return;
    }
    const text = pendingSpeechRef.current;
    if (!text.trim()) {
      return;
    }
    const hasBoundary = /[.!?。！？]\s*$/.test(text);
    const minChunkReached = text.length >= 80;
    if (!forceFlush && !hasBoundary && !minChunkReached) {
      return;
    }
    speakFallback(text.trim(), pendingSpeechLanguageRef.current || "english");
    pendingSpeechRef.current = "";
  }

  // ── WebSocket message handler ─────────────────────────────────────────────
  // useCallback with stable hook-provided deps so handleMessage never changes
  // after mount, preventing WebSocket reconnects.

  const handleMessage = useCallback(
    async (msg) => {
      if (msg.type === "ready") {
        if (msg.runtime) {
          setRuntime((prev) => ({ ...prev, ...msg.runtime }));
        }
        setStatus(msg.runtime?.models_loaded ? "Ready - Hold Space to talk" : "Loading models...");
        return;
      }

      if (msg.type === "runtime_update") {
        if (msg.runtime) {
          setRuntime((prev) => ({ ...prev, ...msg.runtime }));
          if (msg.runtime.models_loaded) {
            setStatus("Ready - Hold Space to talk");
          }
        }
        return;
      }

      if (msg.type === "interrupted") {
        const requestId = msg.request_id || currentRequestIdRef.current;
        if (requestId) {
          interruptedRequestIdsRef.current.add(requestId);
          setChatHistory((prev) =>
            prev.map((item) => (item.id === requestId ? { ...item, interrupted: true } : item)),
          );
        }
        setStatus("Interrupted. Hold Space to talk");
        setTtsStreamActive(false);
        isAssistantStreamingRef.current = false;
        return;
      }

      if (msg.type === "transcript") {
        const requestId = msg.request_id || `${Date.now()}`;
        currentRequestIdRef.current = requestId;
        interruptedRequestIdsRef.current.delete(requestId);
        isAssistantStreamingRef.current = true;
        setTranscript(msg.transcript || "");
        setTranscriptRaw(msg.transcript_raw || "");
        setUserEmotion(msg.transcript_emotion || "neutral");
        setAssistantText("");
        hasPlayedAudioRef.current = false;
        pendingSpeechRef.current = "";
        stopAllAudio();
        setChatHistory((prev) => [
          ...prev,
          { id: requestId, userText: msg.transcript || "", assistantText: "", interrupted: false },
        ]);
        setStatus("Generating response...");
        return;
      }

      if (msg.type === "tool_calls") {
        const requestId = msg.request_id || currentRequestIdRef.current || `${Date.now()}`;
        if (msg.tool_calls && msg.tool_calls.length > 0) {
          setToolCalls((prev) => [
            ...prev,
            { requestId, calls: msg.tool_calls || [] },
          ]);
        }
        if (msg.tool_router_raw) {
          setToolRouterRaw({ requestId, raw: msg.tool_router_raw });
        }
        return;
      }

      if (msg.type === "llm_token") {
        const requestId = msg.request_id || currentRequestIdRef.current;
        if (requestId && interruptedRequestIdsRef.current.has(requestId)) {
          return;
        }
        const token = msg.token || "";
        setAssistantText((prev) => prev + token);
        if (requestId && token) {
          setChatHistory((prev) =>
            prev.map((item) => (item.id === requestId ? { ...item, assistantText: item.assistantText + token } : item)),
          );
        }
        return;
      }

      if (msg.type === "tts_audio_chunk" && msg.audio_base64) {
        const requestId = msg.request_id || currentRequestIdRef.current;
        if (requestId && interruptedRequestIdsRef.current.has(requestId)) {
          return;
        }
        if (msg.tts_voice_id) {
          setRuntime((prev) => ({
            ...prev,
            tts_last_voice_id: msg.tts_voice_id,
            tts_last_voice_reason: msg.tts_voice_reason || prev.tts_last_voice_reason,
            tts_last_text_language: msg.tts_text_language || prev.tts_last_text_language,
          }));
        }
        hasPlayedAudioRef.current = true;
        pendingSpeechRef.current = "";
        if (window.speechSynthesis) {
          window.speechSynthesis.cancel();
        }
        setTtsStreamActive(true);
        await enqueueChunk(msg.audio_base64);
        return;
      }

      if (msg.type === "llm_done") {
        const requestId = msg.request_id || currentRequestIdRef.current;
        if (requestId && interruptedRequestIdsRef.current.has(requestId)) {
          setStatus("Interrupted. Hold Space to talk");
          setTtsStreamActive(false);
          isAssistantStreamingRef.current = false;
          return;
        }
        const shouldFallback = ENABLE_BROWSER_TTS_FALLBACK && !hasPlayedAudioRef.current;
        if (shouldFallback) {
          if (msg.text && !pendingSpeechRef.current.trim()) {
            pendingSpeechRef.current = msg.text;
          }
          pendingSpeechLanguageRef.current = msg.tts_text_language || "english";
          streamFallbackSpeech(true);
        }
        if (requestId && msg.text) {
          setChatHistory((prev) =>
            prev.map((item) => (item.id === requestId ? { ...item, assistantText: msg.text } : item)),
          );
        }
        setStatus("Ready - Hold Space to talk");
        setTtsStreamActive(false);
        isAssistantStreamingRef.current = false;
        return;
      }

      if (msg.type === "error") {
        setStatus(`Error: ${msg.error || "Unknown error"}`);
      }
    },
    // stopAllAudio, enqueueChunk, and setTtsStreamActive are all stable references
    // from useAudioPlayback, so this callback is created exactly once.
    [enqueueChunk, setTtsStreamActive, stopAllAudio],
  );

  const { socketState, send } = useWebSocket(WS_URL, handleMessage);
  const canTalk = useMemo(() => socketState === "connected", [socketState]);

  // ── Interrupt / clear context ─────────────────────────────────────────────

  const interruptActiveResponse = useCallback(async () => {
    const requestId = currentRequestIdRef.current;
    if (requestId) {
      interruptedRequestIdsRef.current.add(requestId);
      send({ type: "interrupt", request_id: requestId });
      setChatHistory((prev) =>
        prev.map((item) => (item.id === requestId ? { ...item, interrupted: true } : item)),
      );
    }
    isAssistantStreamingRef.current = false;
    setStatus("Interrupted. Hold Space to talk");
    stopAllAudio();
    pendingSpeechRef.current = "";
  }, [send, stopAllAudio]);

  const clearLlmContext = useCallback(async () => {
    if (isAssistantStreamingRef.current || ttsStreamActive || window.speechSynthesis?.speaking) {
      await interruptActiveResponse();
    }
    send({ type: "clear_context" });
    currentRequestIdRef.current = null;
    interruptedRequestIdsRef.current.clear();
    setTranscript("");
    setTranscriptRaw("");
    setUserEmotion("neutral");
    setAssistantText("");
    setChatHistory([]);
    setToolCalls([]);
    setToolRouterRaw(null);
    setStatus("Context cleared. Hold Space to talk");
    stopAllAudio();
    pendingSpeechRef.current = "";
    isAssistantStreamingRef.current = false;
  }, [send, ttsStreamActive, interruptActiveResponse, stopAllAudio]);

  // ── Text input ────────────────────────────────────────────────────────────

  const handleSendText = useCallback(
    (text) => {
      // Stop any in-flight audio locally for responsiveness; the backend cancels
      // the active task on user_text and emits "interrupted" for the old turn.
      stopAllAudio();
      pendingSpeechRef.current = "";
      send({ type: "user_text", text });
      setStatus("Generating response...");
    },
    [send, stopAllAudio],
  );

  // ── Recording ─────────────────────────────────────────────────────────────

  const handleRecordingReady = useCallback(
    ({ base64, mimeType }) => {
      send({ type: "user_audio", mime_type: mimeType, audio_base64: base64 });
      setStatus("Transcribing...");
    },
    [send],
  );

  const { isRecording, startRecording, stopRecording } = useRecorder({
    onReady: handleRecordingReady,
    enabled: canTalk,
  });

  // ── Keyboard handler (Space push-to-talk) ─────────────────────────────────
  // All mutable values are accessed through refs so the effect runs only on
  // mount/unmount, avoiding repeated listener re-registration every render.

  const ttsStreamActiveRef = useRef(ttsStreamActive);
  ttsStreamActiveRef.current = ttsStreamActive;
  const ensureAudioContextRef = useRef(ensureAudioContext);
  ensureAudioContextRef.current = ensureAudioContext;
  const interruptActiveResponseRef = useRef(interruptActiveResponse);
  interruptActiveResponseRef.current = interruptActiveResponse;
  const startRecordingRef = useRef(startRecording);
  startRecordingRef.current = startRecording;
  const stopRecordingRef = useRef(stopRecording);
  stopRecordingRef.current = stopRecording;

  useEffect(() => {
    const onKeyDown = async (event) => {
      if (event.code !== "Space") {
        return;
      }
      // Don't hijack Space while the user is typing in the text box.
      const target = event.target;
      const tag = target?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || target?.isContentEditable) {
        return;
      }
      event.preventDefault();

      const shouldInterrupt =
        isAssistantStreamingRef.current ||
        ttsStreamActiveRef.current ||
        hasPendingPlayback() ||
        window.speechSynthesis?.speaking;

      if (shouldInterrupt) {
        if (event.repeat) {
          return;
        }
        isSpacePressedRef.current = true;
        await interruptActiveResponseRef.current();
        await ensureAudioContextRef.current();
        await startRecordingRef.current();
        return;
      }

      if (isSpacePressedRef.current || event.repeat) {
        return;
      }
      isSpacePressedRef.current = true;
      await ensureAudioContextRef.current();
      await startRecordingRef.current();
    };

    const onKeyUp = async (event) => {
      if (event.code !== "Space") {
        return;
      }
      event.preventDefault();
      isSpacePressedRef.current = false;
      await stopRecordingRef.current();
    };

    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("keyup", onKeyUp);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("keyup", onKeyUp);
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Runtime status polling ────────────────────────────────────────────────

  useEffect(() => {
    let cancelled = false;
    const fetchStatus = async () => {
      try {
        const res = await fetch(STATUS_URL);
        if (!res.ok) {
          return;
        }
        const data = await res.json();
        if (!cancelled) {
          setRuntime((prev) => ({ ...prev, ...data }));
        }
      } catch {
        // Ignore transient polling errors.
      }
    };
    fetchStatus();
    const id = setInterval(fetchStatus, 5000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  // ── Render ────────────────────────────────────────────────────────────────

  return (
    <div className="app-shell">
      <header className="hero">
        <div className="hero-top">
          <p className="eyebrow">Hong Kong MTR Inspection Robot</p>
          <button
            type="button"
            className="clear-context-btn"
            onClick={clearLlmContext}
            disabled={socketState !== "connected"}
          >
            Clear LLM Context
          </button>
        </div>
        <h1>{`MTR-Insight · ${runtime.configured_model || "LLM"} Voice Assistant`}</h1>
        <p className="status">{status}</p>
        <div className={`ptt ${isRecording ? "active" : ""}`}>
          <div className="ptt-button">
            <span>SPACE</span>
            <small>{isRecording ? "Release to send" : "Hold to talk / press again to interrupt"}</small>
          </div>
          <StatusPanel runtime={runtime} />
        </div>
      </header>

      <main className="grid">
        <TranscriptCards
          transcript={transcript}
          transcriptRaw={transcriptRaw}
          assistantText={assistantText}
          userEmotion={userEmotion}
          onSendText={handleSendText}
          textInputDisabled={!canTalk}
        />
        <ChatHistory chatHistory={chatHistory} />
        <ImageAnnotator disabled={socketState !== "connected"} />
        <DebugPanel toolCalls={toolCalls} toolRouterRaw={toolRouterRaw} />
        <ReportImageGallery />
      </main>

      <footer className="footer">WebSocket: {socketState}</footer>
    </div>
  );
}
