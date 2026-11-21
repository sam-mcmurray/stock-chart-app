export function toNumericSettings(draft) {
  return Object.fromEntries(
    Object.entries(draft)
      .filter(([, value]) => value !== '' && value !== null && value !== undefined)
      .map(([key, value]) => [key, Number(value)])
  );
}

export function validateScannerSettings(values) {
  const rules = [
    ['price_min', (v) => v === undefined || v >= 0, 'Min price must be 0 or greater.'],
    ['price_max', (v) => v === undefined || v > 0, 'Max price must be greater than 0.'],
    ['rvol_threshold', (v) => v === undefined || v >= 0, 'RVOL must be 0 or greater.'],
    ['rvol_window_days', (v) => v === undefined || (v >= 20 && v <= 252), 'RVOL days must be between 20 and 252.'],
    ['float_max', (v) => v === undefined || v >= 1, 'Max float must be at least 1.'],
    ['scan_interval', (v) => v === undefined || v >= 5, 'Interval must be at least 5 seconds.'],
  ];
  for (const [key, isValid, message] of rules) {
    const value = values[key];
    if (value !== undefined && !Number.isFinite(value)) return `${key} must be a number.`;
    if (!isValid(value)) return message;
  }
  if (values.price_min !== undefined && values.price_max !== undefined && values.price_min > values.price_max) {
    return 'Min price must be less than or equal to max price.';
  }
  return '';
}
