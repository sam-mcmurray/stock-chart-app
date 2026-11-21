// frontend/src/__tests__/useIsMobile.test.jsx
import { renderHook, act } from '@testing-library/react';
import { describe, it, expect, afterEach } from 'vitest';
import useIsMobile from '../hooks/useIsMobile';

// Helper to set the window width and dispatch a resize event
const fireResize = (width) => {
  window.innerWidth = width;
  window.dispatchEvent(new Event('resize'));
};

describe('useIsMobile', () => {

  // Hold original window.innerWidth
  const originalInnerWidth = window.innerWidth;

  afterEach(() => {
    // Restore original window.innerWidth after each test
    window.innerWidth = originalInnerWidth;
  });

  it('should return true if window width is less than the breakpoint', () => {
    window.innerWidth = 500;
    const { result } = renderHook(() => useIsMobile(900));
    expect(result.current).toBe(true);
  });

  it('should return false if window width is greater than the breakpoint', () => {
    window.innerWidth = 1200;
    const { result } = renderHook(() => useIsMobile(900));
    expect(result.current).toBe(false);
  });
  
  it('should return false if window width is equal to the breakpoint', () => {
    window.innerWidth = 900;
    const { result } = renderHook(() => useIsMobile(900));
    expect(result.current).toBe(false);
  });

  it('should update on window resize', () => {
    window.innerWidth = 1200; // Start wide (not mobile)
    const { result } = renderHook(() => useIsMobile(900));
    expect(result.current).toBe(false);

    // Simulate resize to a mobile width
    act(() => {
      fireResize(600);
    });
    expect(result.current).toBe(true);

    // Simulate resize back to a desktop width
    act(() => {
      fireResize(1024);
    });
    expect(result.current).toBe(false);
  });

  it('should use a custom breakpoint', () => {
    window.innerWidth = 700; // Width is 700
    
    // Test with breakpoint 600 (should not be mobile)
    const { result: result1 } = renderHook(({ breakpoint }) => useIsMobile(breakpoint), {
      initialProps: { breakpoint: 600 },
    });
    expect(result1.current).toBe(false);

    // Test with breakpoint 800 (should be mobile)
    const { result: result2 } = renderHook(({ breakpoint }) => useIsMobile(breakpoint), {
      initialProps: { breakpoint: 800 },
    });
    expect(result2.current).toBe(true);
  });
});
