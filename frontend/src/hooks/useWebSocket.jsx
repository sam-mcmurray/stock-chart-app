// frontend/src/hooks/useWebSocket.jsx
import { useEffect, useRef, useState, useCallback } from 'react';

const useWebSocket = (url) => {
  const [isConnected, setIsConnected] = useState(false);
  const [lastMessage, setLastMessage] = useState(null);
  const ws = useRef(null);

  const connect = useCallback(() => {
    if (ws.current && (ws.current.readyState === WebSocket.OPEN || ws.current.readyState === WebSocket.CONNECTING)) {
      return; // Already connected or connecting
    }

    ws.current = new WebSocket(url);

    ws.current.onopen = () => {
      console.log('WebSocket Connected');
      setIsConnected(true);
    };

    ws.current.onmessage = (event) => {
      try {
        const message = JSON.parse(event.data);
        setLastMessage(message);
      } catch (e) {
        console.error('Failed to parse WebSocket message:', e);
        setLastMessage(event.data); // Store raw data if parsing fails
      }
    };

    ws.current.onclose = (event) => {
      console.log('WebSocket Disconnected:', event.code, event.reason);
      setIsConnected(false);
      // Attempt to reconnect after a delay, unless it was a clean close (1000) or auth error (4401)
      if (event.code !== 1000 && event.code !== 4401) {
        setTimeout(connect, 3000); // Reconnect after 3 seconds
      }
    };

    ws.current.onerror = (error) => {
      console.error('WebSocket Error:', error);
      ws.current.close(); // Force close to trigger onclose and reconnect logic
    };
  }, [url]);

  const disconnect = useCallback(() => {
    if (ws.current) {
      console.log('Manually disconnecting WebSocket');
      ws.current.close(1000, 'Manual disconnect'); // Clean close code
      setIsConnected(false);
    }
  }, []);

  useEffect(() => {
    connect();

    return () => {
      if (ws.current) {
        ws.current.close(1000, 'Component unmount');
      }
    };
  }, [connect]);

  // Function to send messages
  const sendMessage = useCallback((message) => {
    if (ws.current && ws.current.readyState === WebSocket.OPEN) {
      ws.current.send(JSON.stringify(message));
    } else {
      console.warn('WebSocket is not open. Cannot send message:', message);
    }
  }, []);

  return { isConnected, lastMessage, sendMessage, connect, disconnect };
};

export default useWebSocket;
