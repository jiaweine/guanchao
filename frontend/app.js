import { installRuntimeGuards } from './runtime.mjs';
import { installInteractionEnhancements } from './interaction.mjs';
import { installCreationGuards } from './creation.mjs';
import { installCaseContextGuards } from './context.mjs';

installRuntimeGuards();
installInteractionEnhancements();
installCreationGuards();
installCaseContextGuards();
await import('./app-core.js');