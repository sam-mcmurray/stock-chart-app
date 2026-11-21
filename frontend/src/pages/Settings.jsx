import { FiSettings } from 'react-icons/fi';
import { useEffect, useState } from 'react';
import { getScannerSettings, updateScannerSettings } from '../lib/api';
import { toNumericSettings, validateScannerSettings } from '../lib/scannerSettings';

export default function Settings(){
  const [settings, setSettings] = useState(null);
  const [status, setStatus] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getScannerSettings()
      .then(setSettings)
      .catch(err => setStatus(err.message));
  }, []);

  const setField = (field, value) => {
    setSettings(prev => ({...prev, [field]: value}));
    setStatus('');
  };

  const save = async () => {
    setSaving(true);
    setStatus('');
    try {
      const numericSettings = toNumericSettings(settings);
      const validationError = validateScannerSettings(numericSettings);
      if (validationError) {
        setStatus(validationError);
        return;
      }
      const saved = await updateScannerSettings(numericSettings);
      setSettings(saved);
      setStatus('Settings saved.');
    } catch (err) {
      setStatus(err.message);
    } finally {
      setSaving(false);
    }
  };

  const saved = status === 'Settings saved.';

  return (
    <section className="panel">
      <h2 className="page-title"><FiSettings /> Settings</h2>
      <p>Scanner preferences.</p>
      {!settings ? (
        <p className={status ? 'state-error' : ''}>{status || 'Loading settings...'}</p>
      ) : (
        <div className="settings-form">
          <NumberField label="Minimum price" value={settings.price_min} onChange={v => setField('price_min', v)} step="0.01" />
          <NumberField label="Maximum price" value={settings.price_max} onChange={v => setField('price_max', v)} step="0.01" />
          <NumberField label="RVOL threshold" value={settings.rvol_threshold} onChange={v => setField('rvol_threshold', v)} step="0.1" />
          <NumberField label="RVOL window days" value={settings.rvol_window_days} onChange={v => setField('rvol_window_days', v)} step="1" />
          <NumberField label="Maximum float shares" value={settings.float_max} onChange={v => setField('float_max', v)} step="100000" />
          <NumberField label="Scan interval seconds" value={settings.scan_interval} onChange={v => setField('scan_interval', v)} step="5" />
          {status && (
            <div
              className={`settings-feedback ${saved ? 'settings-feedback-success' : 'settings-feedback-error'}`}
              role={saved ? 'status' : 'alert'}
            >
              {status}
            </div>
          )}
          <div className="settings-actions">
            <button className="scanner-save-button" onClick={save} disabled={saving}>
              {saving ? 'Saving...' : 'Save'}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}

function NumberField({ label, value, onChange, step }){
  return (
    <label className="settings-field">
      <span>{label}</span>
      <input
        type="number"
        value={value}
        step={step}
        onChange={e => onChange(e.target.value)}
      />
    </label>
  );
}
