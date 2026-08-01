// TEMPLATE — two build targets, both writing into the SAME ui/dist/ (ADR
// Decision 4/6). Selected via `--mode`:
//
//   vite build --mode plugin      -> dist/template.js   (lib mode; the bundle
//                                     contributes.frontend.bundle points at)
//   vite build --mode standalone  -> dist/index.html + assets (a normal app
//                                     build; what __main__.py serves as GET /)
//
// `npm run build` (package.json) runs both, in that order, with
// `emptyOutDir: false` so the second build doesn't wipe the first's output.
import { defineConfig } from 'vite';

export default defineConfig(({ mode }) => {
  if (mode === 'plugin') {
    return {
      build: {
        outDir: 'dist',
        emptyOutDir: false,
        lib: {
          entry: 'src/plugin.js',
          formats: ['es'],
          fileName: () => 'template.js',
        },
        rollupOptions: {
          // react/react-dom are never imported by src/plugin.js — it uses
          // host.React / host.h from window.__AW_PLUGIN_HOST__ instead, so
          // there is exactly ONE React instance shared by core + every
          // installed app (ADR Decision 3b). Listed here as external anyway
          // so a real app whose slot component DOES `import React from
          // 'react'` (e.g. for JSX) knows to externalize it the same way
          // rather than bundling a second copy — swap in whatever
          // React-in-host resolution mechanism aw-frontend documents next
          // to loadComponentPlugin() (src/apps/loadPlugin.js) if you need it.
          external: ['react', 'react-dom'],
        },
      },
    };
  }
  // mode === 'standalone' (also the default `vite build`/`vite`): a normal
  // app build — index.html + src/standalone.js bundled together.
  return {
    build: {
      outDir: 'dist',
      emptyOutDir: false,
    },
  };
});
