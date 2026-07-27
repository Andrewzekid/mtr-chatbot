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
export function useWebSocket(url, onMessage) {
  const [socketState, setSocketState] = useState("connecting");
  const wsRef = useRef(null);
  const reconnectAttemptRef = useRef(0);
  const reconnectTimerRef = useRef(null);
  const onMessageRef = useRef(onMessage);
  const intentionallyClosedRef = useRef(false);
  const pendingMessagesRef = useRef([]);
  onMessageRef.current = onMessage;

  const clearReconnectTimer = useCallback(() => {
    if (reconnectTimerRef.current) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
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

    clearReconnectTimer();
    intentionallyClosedRef.current = false;
    setSocketState("connecting");

    try {
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        reconnectAttemptRef.current = 0;
        setSocketState("connected");
        flushPendingMessages();
      };

      ws.onclose = () => {
        wsRef.current = null;
        if (intentionallyClosedRef.current) {
          setSocketState("disconnected");
          return;
        }

        setSocketState("reconnecting");
        const attempt = reconnectAttemptRef.current;
        const delay = Math.min(1000 * 2 ** attempt, 30000);
        reconnectAttemptRef.current = attempt + 1;

        reconnectTimerRef.current = window.setTimeout(() => {
          connect();
        }, delay);
      };

      ws.onerror = () => {
        // Let onclose handle reconnection logic; mark error only if not already reconnecting.
        if (reconnectTimerRef.current === null && wsRef.current?.readyState !== WebSocket.OPEN) {
          setSocketState("error");
        }
      };

      ws.onmessage = async (event) => {
        try {
          const msg = JSON.parse(event.data);
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
  }, [url, clearReconnectTimer, flushPendingMessages]);

  useEffect(() => {
    connect();

    return () => {
      intentionallyClosedRef.current = true;
      clearReconnectTimer();
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [connect, clearReconnectTimer]);

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
