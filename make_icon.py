"""Generate icon.png + icon.ico for the t3r builds.

Run this from the project folder before building (both build scripts
do it automatically):
    python make_icon.py

It reuses the exact tray-icon drawing routine from t3r.py, rendered at
256 px and exported to every size Windows wants inside a .ico file.
"""
from t3r import create_icon_image

SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main():
    master = create_icon_image(256)
    master.save("icon.png")
    master.save("icon.ico", sizes=SIZES)
    print("Wrote icon.png and icon.ico (" + ", ".join(str(s[0]) for s in SIZES) + ")")


if __name__ == "__main__":
    main()
