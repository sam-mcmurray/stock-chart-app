import { describe, expect, it } from 'vitest';
import { formatBatchSeconds, formatBatchTime } from './newsLlmTiming';

describe('completed news batch timings', () => {
  it('distinguishes missing samples from measured zero', () => {
    expect(formatBatchSeconds(null, 'inference_seconds')).toBe('Not sampled');
    expect(formatBatchSeconds({}, 'inference_seconds')).toBe('Not sampled');
    expect(formatBatchSeconds({ inference_seconds: 0 }, 'inference_seconds')).toBe('0.000s');
    expect(formatBatchSeconds({ selection_seconds: 0.002 }, 'selection_seconds')).toBe('0.002s');
  });
  it('rejects invalid values and timestamps', () => {
    for (const value of [null, undefined, NaN, Infinity, -1, '2']) {
      expect(formatBatchSeconds({ inference_seconds: value }, 'inference_seconds')).toBe('Not sampled');
      expect(formatBatchTime({ completed_at: value })).toBe('Not sampled');
    }
    expect(formatBatchTime({ completed_at: 100 })).toBe(new Date(100000).toLocaleString());
  });
});
