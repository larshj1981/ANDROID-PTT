"""Tjek at VERSION-filen og versionsnumrene i koden er ens (køres i GitHub-bygningen)."""
import pathlib
import re
import sys

root = pathlib.Path(__file__).resolve().parent.parent
want = (root / "VERSION").read_text().strip()
bad = []
for f in ["hub/hub.py", "windows/pttradio_win.py"]:
    m = re.search(r'^VERSION = "([^"]+)"', (root / f).read_text(encoding="utf-8"), re.M)
    if not m or m.group(1) != want:
        bad.append(f"{f}: {m.group(1) if m else 'mangler'}")
if bad:
    sys.exit(f"Versionen i VERSION er {want}, men: " + ", ".join(bad))
print("Version", want, "OK")
