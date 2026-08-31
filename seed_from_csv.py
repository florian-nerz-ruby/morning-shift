"""Deprecated plaintext CSV seed command.

Morning Shift no longer stores raw reservation data. Import the source CSV
through diggies-api's encrypted PMS importer, then configure Morning Shift's
read-only PMS connection and recipient private key.
"""

from __future__ import annotations

def main() -> None:
    raise SystemExit("seed_from_csv.py is disabled: use the encrypted diggies-api PMS importer")


if __name__ == "__main__":
    main()
