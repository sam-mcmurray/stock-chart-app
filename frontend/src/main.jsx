import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App';
import AutoLoginGate from './auth/AutoLoginGate';
import { AuthProvider } from './auth/AuthContext';
import './styles.css';

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <AutoLoginGate>
          <App />
        </AutoLoginGate>
      </AuthProvider>
    </BrowserRouter>
  </React.StrictMode>
);
