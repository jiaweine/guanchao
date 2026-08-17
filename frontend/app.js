import { installRuntimeGuards } from './runtime.mjs';
import { installInteractionEnhancements } from './interaction.mjs';
import { installCreationGuards } from './creation.mjs';

installRuntimeGuards();
installInteractionEnhancements();
installCreationGuards();
await import('./app-core.js');
