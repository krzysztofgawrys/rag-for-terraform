import { defineConfig } from 'vite';

export default defineConfig({
  server: {
    port: 3000,
    proxy: {
      '/health': 'http://localhost:8000',
      '/modules': 'http://localhost:8000',
      '/query': 'http://localhost:8000',
      '/index': 'http://localhost:8000',
      '/webhook': 'http://localhost:8000',
    },
  },
  build: {
    outDir: 'dist',
  },
});
