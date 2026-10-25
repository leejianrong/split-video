import { formatHash, parseHash, type Route } from './route';

/** The current view, mirrored from `location.hash`. Components read `router.current`; links use `hrefTo(...)`. */
class Router {
  current = $state<Route>(parseHash(typeof location === 'undefined' ? '' : location.hash));

  /** Start listening to the back button and hand-edited URLs. Returns the function that stops. */
  listen(): () => void {
    const sync = () => (this.current = parseHash(location.hash));
    sync();
    window.addEventListener('hashchange', sync);
    return () => window.removeEventListener('hashchange', sync);
  }

  /** Go to a view (adds a history entry, like a link). */
  go(route: Route): void {
    const hash = formatHash(route);
    if (location.hash === hash) return;
    location.hash = hash;
  }
}

export const router = new Router();
export { Router };
