import sys
import pysubs2

if len(sys.argv) != 3:
    print("usage: python3 srt_to_animated_ass.py input.srt output.ass")
    sys.exit(1)

src, dst = sys.argv[1], sys.argv[2]
subs = pysubs2.load(src, encoding="utf-8")

# Global style
style = subs.styles["Default"]
style.fontname = "Arial"
style.fontsize = 16
style.primarycolor = pysubs2.Color(255, 255, 255, 0)   # white
style.outlinecolor = pysubs2.Color(0, 0, 0, 0)         # black
style.backcolor = pysubs2.Color(0, 0, 0, 140)          # semi-transparent black
style.bold = False
style.italic = False
style.borderstyle = 1
style.outline = 2.2
style.shadow = 0
style.alignment = 2
style.marginv = 70
style.marginl = 40
style.marginr = 40

for line in subs:
    text = line.text.replace(r"\N ", r"\N").strip()

    # Start slightly larger + transparent, then settle into place.
    # Also a tiny upward move for a more "caption" feel.
    # 00 = opaque, FF = transparent in ASS alpha.
    line.text = (
        r"{"
        r"\an2"
        r"\blur0.6"
        r"\fscx112\fscy112"
        r"\alpha&HAA&"
        r"\t(0,180,\alpha&H00&\fscx100\fscy100)"
        r"\move(0,0,0,-8,0,180)"
        r"}" + text
    )

subs.save(dst)
