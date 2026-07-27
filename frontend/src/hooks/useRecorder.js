import { useCallback, useEffect, useRef, useState } from "react";

function bytesToBase64(bytes) {
  let binary = "";
  const chunk = 0x8000;
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

/**
 * Manages microphone recording via MediaRecorder.
 *
 * The `onReady` callback is stored in a ref so it always has the latest
 * version without restarting effects.
 *
 * @param {object} opts
 * @param {function} opts.onReady - Called with `{ base64, mimeType }` when recording stops.
 * @param {boolean} opts.enabled  - When false, `startRecording` is a no-op.
 * @returns {{ isRecording: boolean, startRecording: function, stopRecording: function }}
 */
export function useRecorder({ onReady, enabled }) {
  const [isRecording, setIsRecording] = useState(false);
  const mediaStreamRef = useRef(null);
  const mediaRecorderRef = useRef(null);
  const chunksRef = useRef([]);
  const onReadyRef = useRef(onReady);
  onReadyRef.current = onReady;

  // A cached MediaStream can become unusable when the tab is backgrounded:
  // Firefox may stop/end its tracks, after which `new MediaRecorder(stream)`
  // or `recorder.start()` throws "The MediaStream is inactive". Treat a
  // stream as alive only if it is active AND has at least one live audio track.
  const isStreamAlive = useCallback((stream) => {
    if (!stream || !stream.active) {
      return false;
    }
    const tracks = stream.getAudioTracks ? stream.getAudioTracks() : stream.getTracks();
    return tracks.length > 0 && tracks.every((t) => t.readyState === "live");
  }, []);

  // Stop and drop a stale stream so the mic is released before we re-acquire.
  const discardStream = useCallback(() => {
    const stream = mediaStreamRef.current;
    if (stream) {
      stream.getTracks().forEach((t) => {
        try {
          t.stop();
        } catch (_) {
          /* ignore */
        }
      });
    }
    mediaStreamRef.current = null;
  }, []);

  const ensureMicStream = useCallback(async () => {
    if (isStreamAlive(mediaStreamRef.current)) {
      return mediaStreamRef.current;
    }
    discardStream();
    mediaStreamRef.current = await navigator.mediaDevices.getUserMedia({ audio: true });
    return mediaStreamRef.current;
  }, [isStreamAlive, discardStream]);

  const startRecording = useCallback(
    async () => {
      if (!enabled || isRecording) {
        return;
      }
      let stream = await ensureMicStream();
      const begin = (recStream) => {
        chunksRef.current = [];
        const recorder = new MediaRecorder(recStream, { mimeType: "audio/webm" });
        mediaRecorderRef.current = recorder;
        recorder.ondataavailable = (e) => {
          if (e.data.size > 0) {
            chunksRef.current.push(e.data);
          }
        };
        recorder.start();
        setIsRecording(true);
      };
      try {
        begin(stream);
      } catch (err) {
        // The cached stream went inactive (e.g. after tab switch). Re-acquire
        // the mic once and retry before surfacing the error to the user.
        // eslint-disable-next-line no-console
        console.warn("MediaRecorder start failed; re-acquiring mic stream:", err);
        discardStream();
        stream = await ensureMicStream();
        begin(stream);
      }
    },
    [enabled, isRecording, ensureMicStream, discardStream],
  );

  const stopRecording = useCallback(async () => {
    if (!isRecording || !mediaRecorderRef.current) {
      // Recording state can desync after a background-tab teardown. Reset the
      // flag so the UI does not get stuck showing "recording".
      if (isRecording) {
        setIsRecording(false);
      }
      return;
    }
    const recorder = mediaRecorderRef.current;
    await new Promise((resolve) => {
      recorder.onstop = resolve;
      recorder.stop();
    });
    const blob = new Blob(chunksRef.current, { type: "audio/webm" });
    const buffer = await blob.arrayBuffer();
    const base64 = bytesToBase64(new Uint8Array(buffer));
    onReadyRef.current({ base64, mimeType: "audio/webm" });
    setIsRecording(false);
  }, [isRecording]);

  // When the tab becomes visible again, validate the cached mic stream and
  // drop it if it died while backgrounded, so the next spacebar press
  // re-acquires a fresh stream instead of throwing "MediaStream is inactive".
  useEffect(() => {
    const onVisibility = () => {
      if (document.visibilityState !== "visible") {
        return;
      }
      if (!isStreamAlive(mediaStreamRef.current)) {
        discardStream();
      }
      // If a recording was left mid-flight by a background teardown, reset.
      if (isRecording && mediaRecorderRef.current?.state !== "recording") {
        setIsRecording(false);
        mediaRecorderRef.current = null;
      }
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
  }, [isStreamAlive, discardStream, isRecording]);

  return { isRecording, startRecording, stopRecording };
}
