import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const directory = path.dirname(fileURLToPath(import.meta.url));
export default defineConfig({
  plugins: [react()],
  envDir: false, // Never discover .env or expose personal credentials.
  base: '/',
  resolve: { alias: { '@': path.join(directory, 'src') } },
  build: { outDir: '../.copilot/frontend-dist', emptyOutDir: false, manifest: true },
  server: { host: '127.0.0.1' },
  test: {
    environment: 'jsdom', globals: true, setupFiles: './src/tests/setup.ts',
    include: ['src/tests/components/TemplateSelector.test.ts', 'src/tests/product-polish.test.tsx', 'src/tests/export-scope.test.tsx', 'src/tests/store/useExportTasksStore.test.ts', 'src/tests/utils.normalizeErrorMessage.test.ts', 'src/tests/save-slide-material.test.tsx', 'src/tests/components/ExportTasksPanel.test.tsx', 'src/tests/admin.test.tsx', 'src/tests/visitor-session.test.tsx', 'src/tests/task-details.test.tsx', 'src/tests/slide-consistency.test.tsx', 'src/tests/product-shell.test.tsx', 'src/tests/region-edit.test.tsx', 'src/tests/capability-alignment.test.tsx', 'src/tests/capability-notice.test.tsx', 'src/tests/outline-stream.test.ts', 'src/tests/store/useProjectStore.test.ts'],
    poolOptions: { threads: { singleThread: true } },
  },
});
