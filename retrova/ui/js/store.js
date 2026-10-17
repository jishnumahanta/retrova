// App-wide state that outlives a single screen (so a view can restore itself on revisit),
// plus a tiny event bus.

export const store = {
  info: null,          // /api/info
  drives: null,        // /api/drives (cached, refreshed by Home / System)
  words: {},           // platform words: trash, finder, computer
  recover: null,       // Recover session, see views/recover.js
  verify: {},          // Verify screen state
  space: {},
  dupes: {},
  cleanup: {},
  find: {},
};

const bus = new EventTarget();
export function emit(name, detail) { bus.dispatchEvent(new CustomEvent(name, { detail })); }
export function on(name, fn) {
  const f = e => fn(e.detail);
  bus.addEventListener(name, f);
  return () => bus.removeEventListener(name, f);
}

export function setPlatformWords(platform) {
  const mac = platform === 'mac', win = platform === 'windows';
  store.words = {
    trash: win ? 'Recycle Bin' : 'Trash',
    finder: mac ? 'Finder' : win ? 'File Explorer' : 'your file manager',
    computer: mac ? 'Mac' : win ? 'PC' : 'computer',
    showIn: mac ? 'Show in Finder' : win ? 'Show in File Explorer' : 'Show in folder',
  };
}
