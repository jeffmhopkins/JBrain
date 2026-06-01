/// <reference types="vite/client" />
/// <reference types="vite-plugin-pwa/client" />

// Injected by Vite `define` at build time (from package.json version).
declare const __PWA_VERSION__: string;

// d3-force-3d ships no type declarations; we only use forceCollide. The graph
// lib (react-force-graph-2d) bundles its own copy at runtime — this just lets us
// import the same collision force to register on its sim.
declare module "d3-force-3d";
