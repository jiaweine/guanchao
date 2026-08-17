import { installRuntimeGuards } from './runtime.mjs';
import { installInteractionEnhancements } from './interaction.mjs';

installRuntimeGuards();
installInteractionEnhancements();
await import('./app-core.js');
