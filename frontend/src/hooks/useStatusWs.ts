import { useEffect, useRef } from 'react';
import { useAppStore } from '../stores/useAppStore';
import { logger } from '../utils/logger';
import { api, type ServerStatusPayload } from '../services/api';

export function useStatusWs() {
  const { setServerStatus, setIsConnected, isAuthenticated } = useAppStore();
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);

  const connect = () => {
    // Only connect if user is authenticated
    if (!useAppStore.getState().isAuthenticated) return;

    if (wsRef.current && (wsRef.current.readyState === WebSocket.OPEN || wsRef.current.readyState === WebSocket.CONNECTING)) {
      return;
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    const wsUrl = `${protocol}//${host}/ws/status`;

    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => {
        setIsConnected(true);
      };

      ws.onmessage = (event) => {
        try {
          const payload: ServerStatusPayload = JSON.parse(event.data);
          setServerStatus(payload);
        } catch (_) {}
      };

      ws.onerror = () => {
        // Benign socket error
      };

      ws.onclose = () => {
        setIsConnected(false);
        wsRef.current = null;

        // Only poll or reconnect if STILL authenticated
        if (useAppStore.getState().isAuthenticated) {
          api.control.getStatus().then((fallback) => {
            if (fallback && useAppStore.getState().isAuthenticated) {
              setServerStatus(fallback);
            }
          });

          reconnectTimeoutRef.current = window.setTimeout(() => {
            if (useAppStore.getState().isAuthenticated) {
              connect();
            }
          }, 3000);
        }
      };
    } catch (err) {
      logger.error('StatusWs', 'Error initiating WebSocket', err);
    }
  };

  useEffect(() => {
    if (isAuthenticated) {
      connect();
    } else {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
        reconnectTimeoutRef.current = null;
      }
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      setIsConnected(false);
    }

    return () => {
      if (reconnectTimeoutRef.current) {
        clearTimeout(reconnectTimeoutRef.current);
        reconnectTimeoutRef.current = null;
      }
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [isAuthenticated]);
}
