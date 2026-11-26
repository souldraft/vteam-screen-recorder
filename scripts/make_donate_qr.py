"""Make the website's donation QR codes (docs/donate/qr-<amount>.png) for a UPI ID.

Usage:  python scripts/make_donate_qr.py name@bank
Uses the same upi_link / upi_qr_png functions as the app, so the website and the app show identical codes.
Needs: pip install segno
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    upi = sys.argv[1].strip()
    spec = importlib.util.spec_from_file_location("vd", os.path.join(ROOT, "VTeamScreenRecorder.py"))
    vd = importlib.util.module_from_spec(spec)
    sys.modules["vd"] = vd
    spec.loader.exec_module(vd)
    if not vd.is_upi_id(upi):
        print(f"'{upi}' doesn't look like a UPI ID (name@bank).")
        return 1
    out = os.path.join(ROOT, "docs", "donate")
    os.makedirs(out, exist_ok=True)
    for amount in vd.DONATE_AMOUNTS:
        link = vd.upi_link(upi, amount)
        with open(os.path.join(out, f"qr-{amount}.png"), "wb") as f:
            f.write(vd.upi_qr_png(link, scale=10))
        print(f"qr-{amount}.png  {link}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
