import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

// The SPA is served by Flask under /outreach (webui/app.py). `base` makes
// built asset URLs resolve there; the dev server proxies API + SSE calls to
// the running Flask backend so `npm run dev` works against real data.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: '/outreach/',
  build: {
    outDir: '../webui/dist',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:5050',
      '/stream': 'http://127.0.0.1:5050',
    },
  },
})
