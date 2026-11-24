"""Write the e-ink screen previews committed under docs/previews."""

from datetime import datetime, timezone
from pathlib import Path

from sideload_pi.display.screens import write_gallery, write_test_patterns

OUT = Path(__file__).resolve().parents[1] / "docs" / "previews"
WHEN = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)


def main() -> None:
    written = write_gallery(OUT, WHEN)
    written.extend(write_test_patterns(OUT))
    for path in written:
        print(path)


if __name__ == "__main__":
    main()
