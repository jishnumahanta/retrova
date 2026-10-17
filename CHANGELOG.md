# Changelog

## 1.0.1 (18 October 2026)

**Retrova is now open source** under the GNU AGPL-3.0. The full source, build scripts and tests are in this repository.

- **New: Support Retrova.** A sidebar entry with ways to chip in (UPI, Wise, GitHub Sponsors) and the licence notice. After a successful recovery or a clean verify there's a quiet "Support Retrova" button. Paying unlocks nothing; every feature stays free. Links open in your own browser, and the app still makes no network connections.
- **Fixed:** the drive-health summary on the home screen said "All 4 drives look healthy" when some drives (such as disk images) can't report health. It now only counts drives that actually reported.
- The app, installer and Windows executable now carry the copyright and AGPL-3.0 notice, and the licence texts ship inside the app.
- Tests no longer trigger the administrator password prompt when run on a Mac or Windows PC.

## 1.0.0 (17 October 2026)

First release: Recover, Verify, Space, Duplicates, Cleanup, Find Text and System for macOS 11+ and Windows 10/11.
