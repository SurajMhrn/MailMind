"""Losslessly-looking PNG compression for the project's images (diagrams, logo, favicon).

Diagrams and icons use few colours, so a 256-colour palette is visually identical
and much smaller. A file is only replaced when the result is smaller.

Run:  python tools/compress_images.py [folder ...]   (needs Pillow: pip install -r requirements-dev.txt)
"""
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIRS = [ROOT / "assets", ROOT / "docs"]


def compress(path: Path) -> tuple[int, int]:
    before = path.stat().st_size
    with Image.open(path) as img:
        img.load()
        has_alpha = img.mode in ("RGBA", "LA") or "transparency" in img.info
        img = img.convert("RGBA" if has_alpha else "RGB")
        method = Image.Quantize.FASTOCTREE if has_alpha else Image.Quantize.MEDIANCUT
        small = img.quantize(colors=256, method=method, dither=Image.Dither.NONE)
    tmp = path.with_suffix(".tmp.png")
    small.save(tmp, optimize=True)
    after = tmp.stat().st_size
    if after < before:
        tmp.replace(path)
        return before, after
    tmp.unlink()
    return before, before


def main(dirs: list[Path]) -> None:
    total_before = total_after = 0
    for d in dirs:
        for path in sorted(d.glob("*.png")):
            before, after = compress(path)
            total_before, total_after = total_before + before, total_after + after
            print(f"{path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}: "
                  f"{before / 1024:,.0f} KB -> {after / 1024:,.0f} KB ({100 - after * 100 // before}% smaller)")
    print(f"total: {total_before / 1024:,.0f} KB -> {total_after / 1024:,.0f} KB")


if __name__ == "__main__":
    main([Path(a) for a in sys.argv[1:]] or DEFAULT_DIRS)
