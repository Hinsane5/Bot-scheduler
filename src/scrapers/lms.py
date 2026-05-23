"""LMS scraper.

Implemented in Phase 3.
"""

from __future__ import annotations


class LMSScraper:
    name = "lms"
    schedule_endpoint_template = (
        "https://func-bm7-schedule-prod.azurewebsites.net"
        "/api/Schedule/Date-v1/{year}-{month}-{day}"
    )


def main() -> None:
    raise SystemExit("LMS scraper is implemented in Phase 3.")


if __name__ == "__main__":
    main()
