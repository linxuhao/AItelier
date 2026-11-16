/** Tool tiers are dsh-director's (../dsh-director/lib/tool-discovery.mjs); this
 *  deployment installs them with its own wording (aitelier-texts.mjs). */
import { installToolDiscovery as install } from '../dsh-director/lib/tool-discovery.mjs';
import { discoveryTexts } from './aitelier-texts.mjs';

export * from '../dsh-director/lib/tool-discovery.mjs';
export const installToolDiscovery = (ctx, options) => install(ctx, { texts: discoveryTexts, ...options });
