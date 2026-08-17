import { installRuntimeGuards } from './runtime.mjs';

installRuntimeGuards();
await import('./app-core.js');
