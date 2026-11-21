// frontend/src/__tests__/useWebSocket.test.jsx
import { renderHook, act } from '@testing-library/react';
import { beforeAll, afterAll, describe, it, expect, vi } from 'vitest';
import useWebSocket from '../hooks/useWebSocket';

// A mock WebSocket class to simulate WebSocket behavior
class MockWebSocket {
  static instances = [];
  
  constructor(url) {
    this.url = url;
    this.readyState = WebSocket.CONNECTING;
    MockWebSocket.instances.push(this);
    
    // Mock methods
    this.send = vi.fn();
    this.close = vi.fn(() => {
      this.readyState = WebSocket.CLOSED;
      if (this.onclose) {
        this.onclose({ code: 1000, reason: 'Manual close' });
      }
    });

    // Event handlers
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
  }

  // Helper methods to simulate server actions
  _open() {
    this.readyState = WebSocket.OPEN;
    if (this.onopen) {
      this.onopen();
    }
  }

  _message(data) {
    if (this.onmessage) {
      this.onmessage({ data: JSON.stringify(data) });
    }
  }
  
  _error() {
      if (this.onerror) {
          this.onerror(new Error("Fake WebSocket Error"));
      }
  }
}

describe('useWebSocket', () => {
  const testUrl = 'ws://localhost:8080';

  beforeAll(() => {
    // Replace the global WebSocket with our mock
    globalThis.WebSocket = MockWebSocket;
    // Mock console to prevent logging during tests
    vi.spyOn(console, 'log').mockImplementation(() => {});
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterAll(() => {
    // Restore original implementations
    vi.restoreAllMocks();
    delete globalThis.WebSocket;
  });

  it('should connect and update isConnected state', () => {
    const { result } = renderHook(() => useWebSocket(testUrl));
    
    // Initial state
    expect(result.current.isConnected).toBe(false);

    // Get the instance and simulate opening the connection
    const wsInstance = MockWebSocket.instances[MockWebSocket.instances.length - 1];
    act(() => {
      wsInstance._open();
    });

    expect(result.current.isConnected).toBe(true);
  });

  it('should receive messages and update lastMessage state', () => {
    const { result } = renderHook(() => useWebSocket(testUrl));
    const wsInstance = MockWebSocket.instances[MockWebSocket.instances.length - 1];
    
    act(() => {
      wsInstance._open();
    });
    
    const message = { type: 'GREETING', payload: 'hello' };
    act(() => {
      wsInstance._message(message);
    });

    expect(result.current.lastMessage).toEqual(message);
  });

  it('should update isConnected state on close', () => {
    const { result } = renderHook(() => useWebSocket(testUrl));
    const wsInstance = MockWebSocket.instances[MockWebSocket.instances.length - 1];
    
    act(() => {
      wsInstance._open();
    });
    expect(result.current.isConnected).toBe(true);
    
    act(() => {
      wsInstance.close();
    });
    
    expect(result.current.isConnected).toBe(false);
  });

  it('should allow sending messages when connected', () => {
    const { result } = renderHook(() => useWebSocket(testUrl));
    const wsInstance = MockWebSocket.instances[MockWebSocket.instances.length - 1];

    act(() => {
      wsInstance._open();
    });
    
    const messageToSend = { command: 'ECHO' };
    act(() => {
      result.current.sendMessage(messageToSend);
    });

    expect(wsInstance.send).toHaveBeenCalledWith(JSON.stringify(messageToSend));
  });

  it('should handle errors and trigger close', () => {
    const { result } = renderHook(() => useWebSocket(testUrl));
    const wsInstance = MockWebSocket.instances[MockWebSocket.instances.length - 1];

    act(() => {
        wsInstance._open();
    });
    expect(result.current.isConnected).toBe(true);
    
    act(() => {
        wsInstance._error();
    });

    expect(wsInstance.close).toHaveBeenCalled();
    expect(result.current.isConnected).toBe(false);
  });
});
