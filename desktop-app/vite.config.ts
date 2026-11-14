import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  plugins: [svelte()],
  clearScreen: false,
  server: { port: 1421, strictPort: true },
  preview: { port: 1421, strictPort: true },
  build: { target: 'es2022' },
});
