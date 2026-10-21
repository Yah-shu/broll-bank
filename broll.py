"""Entry point: python broll.py <command>   (python broll.py --help)"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from broll.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
