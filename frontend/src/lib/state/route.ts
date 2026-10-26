// Views and the hash (ADR 0004, "Routing"). There is no router: the hash is the only thing in the URL, and this
// module is the only code that reads `location`. Formats:
//   #/                       start (first run)
//   #/library                the library at the server's root folder
//   #/library/<folder>       the library in a folder (percent-encoded, '/' kept)
//   #/p/<project id>         the editor with that project open (the id is the video's path, percent-encoded)
//   #/dev                    the components gallery (not linked)

export type Route =
  | { view: 'start' }
  | { view: 'library'; dir: string }
  | { view: 'editor'; id: string }
  | { view: 'dev' };

const decode = (text: string): string | null => {
  try {
    return decodeURIComponent(text);
  } catch {
    return null;
  }
};

const encodePath = (path: string) => path.split('/').map(encodeURIComponent).join('/');

/** Parse `location.hash`. Anything unknown or malformed is the start view, never an error. */
export function parseHash(hash: string): Route {
  const path = hash.replace(/^#\/?/, '');
  if (path === 'dev') return { view: 'dev' };
  if (path === 'library') return { view: 'library', dir: '' };
  if (path.startsWith('library/')) {
    const dir = decode(path.slice('library/'.length));
    return dir === null ? { view: 'start' } : { view: 'library', dir };
  }
  if (path.startsWith('p/')) {
    const id = decode(path.slice('p/'.length));
    return id ? { view: 'editor', id } : { view: 'start' };
  }
  return { view: 'start' };
}

export function formatHash(route: Route): string {
  switch (route.view) {
    case 'start':
      return '#/';
    case 'library':
      return route.dir ? `#/library/${encodePath(route.dir)}` : '#/library';
    case 'editor':
      return `#/p/${encodePath(route.id)}`;
    case 'dev':
      return '#/dev';
  }
}

export const hrefTo = formatHash;
