// "Support Retrova": ways to chip in (UPI, Wise, GitHub Sponsors) plus the licence notice.
// Retrova makes no network connections; links open in the user's own browser via the engine,
// which only accepts the fixed link ids listed in recovery/api_platform.py.
import { h } from '../util.js';
import { api } from '../api.js';
import { icon } from './icons.js';
import { openSheet } from './sheet.js';
import { toast, toastError } from './toast.js';
import { store } from '../store.js';

const UPI_ID = '8472066376-4@ybl';

export const openLink = id => api.post('/api/open_link', { id }).catch(e => toastError(e));

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = h('textarea', { value: text, style: 'position:fixed;opacity:0' });
    document.body.append(ta);
    ta.select();
    document.execCommand('copy');
    ta.remove();
  }
  toast('UPI ID copied', { kind: 'ok', timeout: 2500 });
}

export function openSupport() {
  const version = store.info?.version || '';
  const body = h('div.support',
    h('p.support-lead', 'Retrova is free, with no limits, and built by one developer in Guwahati, India. If it got your files back or saved you some space, a small thank-you keeps it going.'),
    h('div.support-grid',
      h('section.support-opt.support-upi',
        h('img.support-qr', { src: 'img/upi-qr.svg', width: 148, height: 148, alt: 'UPI QR code' }),
        h('div.support-opt-body',
          h('h3', 'UPI (India)'),
          h('p', 'Scan with Google Pay, PhonePe, Paytm or any UPI app.'),
          h('div.support-upi-id', h('code.selectable', { text: UPI_ID }),
            h('button.btn.sm', { onclick: () => copyText(UPI_ID) }, icon('copy'), 'Copy')))),
      h('section.support-opt',
        h('h3', 'From anywhere'),
        h('p', 'Card or bank transfer in your own currency.'),
        h('div.support-btns',
          h('button.btn.primary', { onclick: () => openLink('wise') }, icon('heart'), 'Pay with Wise'),
          h('button.btn', { onclick: () => openLink('sponsor') }, icon('heart'), 'GitHub Sponsors')))),
    h('div.support-free',
      h('span', 'Free ways to help:'),
      h('button.btn.ghost.sm', { onclick: () => openLink('source') }, icon('sparkle'), 'Star it on GitHub'),
      h('button.btn.ghost.sm', { onclick: () => openLink('issues') }, icon('info'), 'Report a problem')),
    h('p.support-legal',
      `Retrova ${version}. Copyright © 2026 Jishnu Mahanta. Free software under the GNU AGPL-3.0, with no warranty. `,
      h('a', { href: '#', onclick: e => { e.preventDefault(); openLink('source'); } }, 'Source code'), '.'));
  openSheet({
    title: 'Support Retrova', subtitle: 'Paying unlocks nothing. Every feature is already free.', icon: 'heart',
    body, size: 'wide', closeButton: true, actions: [{ label: 'Close', autofocus: true }],
  });
}

/** A quiet button for "it worked" moments (recovery finished, copy verified). */
export const supportButton = () => h('button.btn.ghost.support-nudge', { onclick: openSupport, title: 'Retrova is free. Support it if it helped.' },
  icon('heart'), 'Support Retrova');
