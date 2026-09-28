import sys

if sys.version_info < (3, 11):
    sys.stderr.write("meeting-notetaker needs Python 3.11 or newer.\n")
    sys.exit(1)

from notetaker.cli import main  # noqa: E402

sys.exit(main())
