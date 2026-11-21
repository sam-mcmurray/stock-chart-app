import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'

const certFile = process.env.VITE_TLS_CERT_FILE || '/certs/server.crt'
const keyFile = process.env.VITE_TLS_KEY_FILE || '/certs/server.key'
const localCertFile = path.resolve('../certs/server.crt')
const localKeyFile = path.resolve('../certs/server.key')

function httpsConfig() {
  const cert = fs.existsSync(certFile) ? certFile : localCertFile
  const key = fs.existsSync(keyFile) ? keyFile : localKeyFile
  if (!fs.existsSync(cert) || !fs.existsSync(key)) return undefined
  return {
    cert: fs.readFileSync(cert),
    key: fs.readFileSync(key),
  }
}

export default defineConfig({
  plugins: [react()],
  optimizeDeps: { include: ['lightweight-charts'] },
  server: {
    host: true, // 0.0.0.0
    port: 3000,
    https: httpsConfig(),
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/setupTests.js', // We will create this file next
  },
})
