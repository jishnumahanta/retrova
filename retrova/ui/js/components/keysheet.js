// "This drive is locked": asks for the password / recovery key of an encrypted drive.
// The text typed here goes to the engine once, in the request that tries it. It is never stored
// (not in the store, not in the URL, not in localStorage) and the field is emptied as soon as it is used.
import { h } from '../util.js';
import { icon } from './icons.js';
import { openSheet } from './sheet.js';
import { openLink } from './support.js';

const WORDS = {
  bitlocker: { name: 'BitLocker', both: 'Password or recovery key', password: 'Password', recovery: 'Recovery key (48 digits)' },
  luks: { name: 'LUKS', password: 'Passphrase' },
  apfs: { name: 'FileVault', both: 'Password or recovery key', password: 'Password' },
};

function inputLabel(kind, accepts) {
  const w = WORDS[kind] || WORDS.bitlocker;
  const both = accepts.includes('password') && accepts.includes('recovery');
  if (both) return w.both || 'Password or recovery key';
  return accepts.includes('recovery') && !accepts.includes('password') ? w.recovery || 'Recovery key' : w.password;
}

/** Where to find the key, in plain words. */
function whereToFind(kind, accepts) {
  if (kind === 'bitlocker') {
    const bits = [];
    if (accepts.includes('recovery')) {
      bits.push(h('p.hint', 'Lost it? The 48-digit recovery key is often saved in your Microsoft account, in your work or school account, or on a printout. ',
        h('a', { href: '#', onclick: e => { e.preventDefault(); openLink('bitlocker_key'); } }, 'aka.ms/myrecoverykey'), ' (opens in your browser).'));
    }
    return bits;
  }
  if (kind === 'apfs') return [h('p.hint', 'Use the password of a Mac account that can unlock this drive, or the FileVault recovery key you were given when it was turned on.')];
  return [];
}

/**
 * askForKey({ kind, label, hint, accepts, driveName, attempt })
 *   attempt(key) -> Promise<result>; throw to show the message and let them retry.
 * Resolves with attempt's result, or null if they cancel.
 */
export function askForKey({ kind, label, hint, accepts = ['password'], driveName, attempt }) {
  const w = WORDS[kind] || WORDS.bitlocker;
  return new Promise(resolve => {
    let result = null;
    const field = h('input.field', { type: 'password', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off', 'aria-label': inputLabel(kind, accepts), style: 'padding-right:40px' });
    const eye = h('button.btn.icon.ghost.sm.key-eye', { type: 'button', 'aria-label': 'Show what I typed', 'aria-pressed': 'false', title: 'Show what I typed', style: 'position:absolute;right:4px' }, icon('eye'));
    eye.addEventListener('click', () => {
      const show = field.type === 'password';
      field.type = show ? 'text' : 'password';
      eye.replaceChildren(icon(show ? 'eyeOff' : 'eye'));
      eye.setAttribute('aria-pressed', String(show));
      eye.title = eye.ariaLabel = show ? 'Hide what I typed' : 'Show what I typed';
      field.focus();
    });
    const error = h('div.callout.bad', { hidden: true, role: 'alert' }, icon('error'), h('div.grow'));
    const busy = h('p.hint', { hidden: true, role: 'status' }, 'Checking the key. Locked drives make this slow on purpose, so it can take a few seconds…');
    const showError = msg => { error.querySelector('.grow').textContent = msg; error.hidden = false; };
    const sheet = openSheet({
      title: `Unlock ${driveName || label || `this ${w.name} drive`}`,
      subtitle: `This drive is locked with ${w.name}. Enter its key to look at the files on it.`,
      icon: 'lock',
      size: '',
      body: h('div.key-form',
        hint ? h('p.hint', { text: hint }) : null,
        h('label.key-label', { style: 'display:block;font-weight:600;margin:10px 0 6px' }, inputLabel(kind, accepts)),
        h('div.field-wrap', field, eye),
        h('div', { style: 'margin-top:10px' }, error, busy),
        ...whereToFind(kind, accepts)),
      footNote: 'Retrova only reads the drive. Your key is used once, kept in memory only, and forgotten when you leave this drive.',
      dismissible: true,
      onClose: () => { field.value = ''; resolve(result); },
      actions: [
        { label: 'Cancel', value: null },
        {
          label: 'Unlock', kind: 'primary', icon: 'unlock',
          onClick: async () => {
            const key = field.value;
            if (!key) { showError('Type the key first.'); field.focus(); return false; }
            error.hidden = true;
            busy.hidden = false;
            try {
              result = await attempt(key);
            } catch (e) {
              showError(e?.message || "That didn't work.");
              setTimeout(() => { field.focus(); field.select(); }, 0);   // after the button re-enables
              return false;
            } finally {
              busy.hidden = true;
              if (result) field.value = '';
            }
            return true;
          },
          close: true,
        },
      ],
    });
    field.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); sheet.el.querySelector('.sheet-foot .btn.primary')?.click(); }
    });
    field.focus();
  });
}
