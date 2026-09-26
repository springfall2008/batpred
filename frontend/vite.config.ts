import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  base: './',
  plugins: [react()],

  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:5052',
        changeOrigin: true,
      },

      '/metrics': {
        target: 'http://localhost:5052',
        changeOrigin: true,
      },

      '/plan_override': {
        target: 'http://localhost:5052',
        changeOrigin: true,
      },

      '/rate_override': {
        target: 'http://localhost:5052',
        changeOrigin: true,
      },
    },
  },
})
