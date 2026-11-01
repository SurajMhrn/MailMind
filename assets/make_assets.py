"""Generates the logo and favicon SVGs (render to PNG with headless Edge, then compress).

Run:  .venv\\Scripts\\python assets\\make_assets.py
"""
from pathlib import Path

OUT = Path(__file__).parent
PURPLE, LIGHT = "#534AB7", "#7F77DD"


def mark(x: float, y: float, s: float) -> str:
    """Rounded purple tile with a white envelope and a small spark (the agent)."""
    k = s / 64
    return f"""<g transform="translate({x} {y}) scale({k})">
  <rect width="64" height="64" rx="14" fill="{PURPLE}"/>
  <rect x="12" y="19" width="40" height="28" rx="4" fill="none" stroke="#fff" stroke-width="4"/>
  <path d="M14 22 L32 36 L50 22" fill="none" stroke="#fff" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>
  <path d="M50 6 L52.6 12.4 L59 15 L52.6 17.6 L50 24 L47.4 17.6 L41 15 L47.4 12.4 Z" fill="#FAC775"/>
</g>"""


favicon = f'<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64">{mark(0, 0, 64)}</svg>'
# Wordmark colour reads on both the light and the dark Streamlit theme.
logo = f"""<svg xmlns="http://www.w3.org/2000/svg" width="320" height="64" viewBox="0 0 320 64">
{mark(0, 4, 56)}
<text x="68" y="42" font-family="Segoe UI, Arial, sans-serif" font-size="27" font-weight="700" fill="{LIGHT}">Cold Mail Agent</text>
</svg>"""

for name, svg in (("favicon", favicon), ("logo", logo)):
    (OUT / f"{name}.svg").write_text(svg, encoding="utf-8")
    (OUT / f"_{name}.html").write_text(
        f'<!doctype html><html><body style="margin:0;background:transparent">{svg}</body></html>', encoding="utf-8")
print("wrote favicon.svg, logo.svg")
