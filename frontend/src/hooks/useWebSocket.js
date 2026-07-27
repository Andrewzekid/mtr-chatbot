import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Manages a WebSocket connection lifecycle with automatic reconnect and a
 * message send queue.
 *
 * The `onMessage` callback is stored in a ref so the latest version is always
 * called without reconnecting the socket when the handler changes.
 *
 * @param {string} url - WebSocket URL to connect to.
 * @param {function} onMessage - Async function called with each parsed message object.
 * @returns {{ socketState: string, send: function }}
 */
const HEARTBEAT_INTERVAL_MS = 15000; // keep connection alive during idle periods
const HEARTBEAT_GRACE_MS = 5000; // how late a pong can be before we treat it as missing

export function useWebSocket(url, onMessage) {
  const [socketState, setSocketState] = useState("connecting");
  const wsRef = useRef(null);
  const reconnectAttemptRef = useRef(0);
  const reconnectTimerRef = useRef(null);
  const heartbeatTimerRef = useRef(null);
  const heartbeatTimeoutRef = useRef(null);
  const lastPongRef = useRef(Date.now());
  const onMessageRef = useRef(onMessage);
  const intentionallyClosedRef = useRef(false);
  const pendingMessagesRef = useRef([]);
  onMessageRef.current = onMessage;

  const clearTimers = useCallback(() => {
    if (reconnectTimerRef.current) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    if (heartbeatTimerRef.current) {
      window.clearInterval(heartbeatTimerRef.current);
      heartbeatTimerRef.current = null;
    }
    if (heartbeatTimeoutRef.current) {
      window.clearTimeout(heartbeatTimeoutRef.current);
      heartbeatTimeoutRef.current = null;
    }
  }, []);

  const flushPendingMessages = useCallback(() => {
    while (pendingMessagesRef.current.length && wsRef.current?.readyState === WebSocket.OPEN) {
      const data = pendingMessagesRef.current.shift();
      try {
        wsRef.current.send(typeof data === "string" ? data : JSON.stringify(data));
      } catch (err) {
        // Re-queue on send failure and stop flushing.
        pendingMessagesRef.current.unshift(data);
        break;
      }
    }
  }, []);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.CONNECTING || wsRef.current?.readyState === WebSocket.OPEN) {
      return;
    }

    clearTimers();
    intentionallyClosedRef.current = false;
    setSocketState("connecting");

    try {
      const ws = new WebSocket(url);
      wsRef.current = ws;

      const sendPing = () => {
        if (ws.readyState === WebSocket.OPEN) {
          try {
            ws.send(JSON.stringify({ type: "ping", timestamp: Date.now() }));
          } catch (err) {
            // eslint-disable-next-line no-console
            console.warn("WebSocket ping failed:", err);
          }
        }
      };

      const startHeartbeat = () => {
        lastPongRef.current = Date.now();
        heartbeatTimerRef.current = window.setInterval(() => {
          const sinceLastPong = Date.now() - lastPongRef.current;
          if (sinceLastPong > HEARTBEAT_INTERVAL_MS + HEARTBEAT_GRACE_MS) {
            // eslint-disable-next-line no-console
            console.warn(`WebSocket heartbeat missed (last pong ${sinceLastPong}ms ago); closing to reconnect.`);
            ws.close(1001, "heartbeat missed");
            return;
          }
          sendPing();
        }, HEARTBEAT_INTERVAL_MS);
      };

      ws.onopen = () => {
        reconnectAttemptRef.current = 0;
        setSocketState("connected");
        flushPendingMessages();
        startHeartbeat();
      };

      ws.onclose = (event) => {
        clearTimers();
        wsRef.current = null;
        // eslint-disable-next-line no-console
        console.log(`WebSocket closed code=${event.code} reason=${event.reason || "(none)"} wasClean=${event.wasClean}`);
        if (intentionallyClosedRef.current) {
          setSocketState("disconnected");
          return;
        }

        setSocketState("reconnecting");
        const attempt = reconnectAttemptRef.current;
        // Base 1s delay plus exponential backoff, capped at 30s.
        const delay = Math.min(1000 + 1000 * 2 ** attempt, 30000);
        reconnectAttemptRef.current = attempt + 1;

        reconnectTimerRef.current = window.setTimeout(() => {
          connect();
        }, delay);
      };

      ws.onerror = (event) => {
        // Let onclose handle reconnection logic; mark error only if not already reconnecting.
        // eslint-disable-next-line no-console
        console.warn("WebSocket error:", event);
        if (reconnectTimerRef.current === null && wsRef.current?.readyState !== WebSocket.OPEN) {
          setSocketState("error");
        }
      };

      ws.onmessage = async (event) => {
        try {
          const msg = JSON.parse(event.data);
          if (msg.type === "pong" || msg.type === "ping") {
            lastPongRef.current = Date.now();
            return;
          }
          await onMessageRef.current(msg);
        } catch (err) {
          // Non-fatal parse or handler error; keep the connection alive.
          // eslint-disable-next-line no-console
          console.error("WebSocket message handler error:", err);
        }
      };
    } catch (err) {
      setSocketState("error");
      // Retry on initialization failure as well.
      reconnectTimerRef.current = window.setTimeout(() => {
        connect();
      }, 2000);
    }
  }, [url, clearTimers, flushPendingMessages]);

  useEffect(() => {
    connect();

    // When the tab is backgrounded, Firefox throttles setTimeout/setInterval
    // heavily, so a reconnect scheduled by onclose may not fire for a long
    // time. On return to the tab, force an immediate reconnect if the socket
    // is not already open so push-to-talk works right away.
    const onVisibility = () => {
      if (document.visibilityState !== "visible") {
        return;
      }
      if (wsRef.current?.readyState !== WebSocket.OPEN) {
        // Cancel any pending (throttled) reconnect and connect now.
        if (reconnectTimerRef.current) {
          window.clearTimeout(reconnectTimerRef.current);
          reconnectTimerRef.current = null;
        }
        connect();
      }
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      intentionallyClosedRef.current = true;
      clearTimers();
      document.removeEventListener("visibilitychange", onVisibility);
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [connect, clearTimers]);

  const send = useCallback((data) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      try {
        wsRef.current.send(typeof data === "string" ? data : JSON.stringify(data));
        return;
      } catch (err) {
        // Fall through to queue the message for the next reconnect.
      }
    }
    pendingMessagesRef.current.push(data);
  }, []);

  return { socketState, send };
}
