import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'
import { readdir, rm, stat } from 'node:fs/promises'
import path from 'node:path'

// Replace only the schema package's renderer; CodeMirror keeps its own highlighting.
const schemaMarkdown = {
  name: 'schema-help-markdown',
  enforce: 'pre' as const,
  resolveId(source: string, importer?: string) {
    if (/^\.\.\/utils\/markdown(?:\.js)?$/.test(source) && importer?.replaceAll('\\', '/').includes('/@invopop/codemirror-json-schema/dist/')) {
      return fileURLToPath(new URL('./src/utils/editorMarkdown.ts', import.meta.url))
    }
  },
}

const uiDirectory = fileURLToPath(new URL('../apps/predbat/', import.meta.url))
const uiFilename = /^predbat_ui(?:_[A-Za-z0-9_.-]+)?\.(?:html|js|css|webp|svg|png|woff2)$/
const uiFiles: Plugin = {
  name: 'flat-predbat-ui',
  enforce: 'post',
  apply: (config, environment) => environment.command === 'build' && !config.build?.lib,
  async buildStart() {
    const publicDirectory = fileURLToPath(new URL('./public/', import.meta.url))
    for (const entry of await readdir(publicDirectory, { withFileTypes: true })) {
      if (!entry.isFile() || !uiFilename.test(entry.name) || !(await stat(path.join(publicDirectory, entry.name))).size) throw new Error(`Unexpected or empty public asset: ${entry.name}`)
    }
    // Preserve the backend; only this build's reserved filenames may be replaced.
    for (const entry of await readdir(uiDirectory, { withFileTypes: true })) {
      if (entry.isFile() && uiFilename.test(entry.name)) await rm(path.join(uiDirectory, entry.name))
    }
  },
  generateBundle: {
    order: 'post',
    handler(_options, bundle) {
      const html = bundle['index.html']
      if (!html || html.type !== 'asset' || !html.source.length) throw new Error('Modern UI entry point was not generated')
      this.emitFile({ type: 'asset', fileName: 'predbat_ui.html', source: html.source })
      delete bundle['index.html']
      for (const output of Object.values(bundle)) {
        if (output.fileName === 'index.html') continue
        if (!uiFilename.test(output.fileName)) throw new Error(`Unexpected UI filename: ${output.fileName}`)
        if (!(output.type === 'chunk' ? output.code.length : output.source.length)) throw new Error(`Empty UI file: ${output.fileName}`)
      }
    },
  },
}

export default defineConfig({
  base: './',
  plugins: [react(), schemaMarkdown, uiFiles],
  optimizeDeps: { rolldownOptions: { plugins: [schemaMarkdown] } },
  build: {
    outDir: uiDirectory,
    emptyOutDir: false,
    cssCodeSplit: false,
    rolldownOptions: {
      output: {
        entryFileNames: 'predbat_ui.js',
        chunkFileNames: 'predbat_ui_[name].js',
        assetFileNames: 'predbat_ui_[name][extname]',
        codeSplitting: {
          groups: [{
            name: 'pages',
            test: /\/src\/pages\/(?:Docs|Log|Components|Internals|Config|Compare|Annual|Chat|Apps|Entities|Discovery|Cards)Page\.tsx$/,
          }],
        },
      },
    },
  },

  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:5052',
        changeOrigin: true,
      },

      '/apps_value': {
        target: 'http://localhost:5052',
        changeOrigin: true,
      },

      '^/apps(?:\\?.*)?$': {
        target: 'http://localhost:5052',
        changeOrigin: true,
      },

      '^/(?:chat|annual|component_restart|download|debug_)[^/]*(?:/.*)?$': {
        target: 'http://localhost:5052',
        changeOrigin: true,
      },

      '/compare': {
        target: 'http://localhost:5052',
        changeOrigin: true,
      },

      '/images': {
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
