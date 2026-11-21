export function formatBatchSeconds(sample, field) {
  const value = sample?.[field];
  if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return 'Not sampled';
  return `${value.toFixed(3)}s`;
}

export function formatBatchTime(sample) {
  const value = sample?.completed_at;
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) return 'Not sampled';
  return new Date(value * 1000).toLocaleString();
}
