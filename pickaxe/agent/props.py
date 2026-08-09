#!/usr/bin/env python3
"""Set keys in a server.properties file, preserving everything else.

Usage: props.py <file> KEY=VALUE [KEY=VALUE ...]

Done in Python rather than sed because values (MOTD especially) routinely
contain characters that would break a sed expression.
"""

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    path = Path(argv[1])
    updates: dict[str, str] = {}
    for pair in argv[2:]:
        key, _, value = pair.partition("=")
        updates[key] = value

    lines = path.read_text().splitlines() if path.exists() else []
    out: list[str] = []
    seen: set[str] = set()

    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in line:
            key = line.split("=", 1)[0]
            if key in updates:
                if key not in seen:
                    out.append(f"{key}={updates[key]}")
                    seen.add(key)
                continue  # drop duplicate definitions of a managed key
        out.append(line)

    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")

    path.write_text("\n".join(out).rstrip("\n") + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
