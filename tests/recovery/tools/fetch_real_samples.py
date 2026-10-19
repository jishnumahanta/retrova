"""Download real sample files for the optional real-file carving test.

usage: python fetch_real_samples.py <folder> <list.txt>

list.txt has one `url` per line (blank lines and # comments are skipped). Only use files whose
licence allows it (raw.pixls.us shows the licence next to each file: pick CC0 / public domain).
Files are saved by the name at the end of the URL; existing files are kept. Then run:
    RETROVA_REAL_SAMPLES=<folder> pytest tests/recovery/test_carve_real.py
"""
import os
import sys
import urllib.parse
import urllib.request


def main(folder, listing):
    os.makedirs(folder, exist_ok=True)
    for line in open(listing, encoding="utf-8"):
        url = line.strip()
        if not url or url.startswith("#"):
            continue
        name = os.path.basename(urllib.parse.unquote(urllib.parse.urlparse(url).path))
        dst = os.path.join(folder, name)
        if os.path.exists(dst):
            continue
        with urllib.request.urlopen(url, timeout=120) as r, open(dst + ".part", "wb") as fh:
            fh.write(r.read())
        os.replace(dst + ".part", dst)
        print("saved", dst)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(*sys.argv[1:])
