#!/usr/bin/env python3
"""Generate fake historical data for testing the status page."""

import csv
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).parent.parent
HISTORY_DIR = ROOT_DIR / "data"
HISTORY_TICKS = 30

# All service names from main.py
SERVICES = [
    "astrobotany",
    "portal",
    "hngopher",
    "www-homepage",
    "ascii-gallery",
    "git",
    "emporium",
    "spring83",
    "shiftjis-art",
    "goodvibes",
    "license",
    "gopher-homepage",
    "cocktails",
    "flask-gopher",
    "gopher-z",
    "nex-homepage",
    "finger",
    "spartan-homepage",
    "gemini-homepage",
    "gemini-chat",
    "cso",
    "telnet",
]


def generate_data():
    """Generate fake historical data with various edge cases."""
    now = datetime.now(timezone.utc)

    # Generate data for the past 30 days
    for day_offset in range(HISTORY_TICKS):
        day = now - timedelta(days=day_offset)
        year = day.strftime("%Y")
        date_str = day.strftime("%Y%m%d")

        # Create year directory and file path: data/2025/checks-20250114.csv
        year_dir = HISTORY_DIR / year
        year_dir.mkdir(parents=True, exist_ok=True)
        history_file = year_dir / f"checks-{date_str}.csv"

        print(f"Generating data for {date_str}...")

        with history_file.open("w", newline="") as fp:
            writer = csv.writer(fp)

            # Generate ~24 checks per service per day (one per hour)
            for hour in range(24):
                check_time = day.replace(hour=hour, minute=random.randint(0, 59))
                timestamp = int(check_time.timestamp())

                for service in SERVICES:
                    # Create different scenarios for different services
                    success = get_success_for_service(service, day_offset, hour)
                    if success is None:
                        continue

                    success_str = "Y" if success else "N"
                    writer.writerow([timestamp, service, success_str])


def get_success_for_service(service: str, day_offset: int, hour: int) -> bool | None:
    """Determine success based on service and time to create interesting patterns."""

    # Edge case 1: Service that's always operational
    if service == "www-homepage":
        return True

    # Edge case 2: Service that's always down
    if service == "telnet" and day_offset < 5:
        return False

    # Edge case 3: Service with degraded performance (90% uptime)
    if service == "portal":
        return random.random() > 0.1

    # Edge case 4: Service that was down but recovered recently
    if service == "cso":
        if day_offset > 7:
            return False  # Was down 7+ days ago
        else:
            return True  # Recovered in the last 7 days

    # Edge case 5: Service with intermittent issues (cyclical pattern)
    if service == "gemini-chat":
        # Fails every 6 hours
        return hour % 6 != 0

    # Edge case 6: Service that just went down
    if service == "spring83":
        if day_offset < 2:
            return False  # Down for the last 2 days
        else:
            return True  # Was working before

    # Edge case 7: Service with recent degradation (95% -> 85% uptime)
    if service == "astrobotany":
        if day_offset < 5:
            return random.random() > 0.15  # 85% uptime recently
        else:
            return random.random() > 0.05  # 95% uptime before

    # Edge case 8: No data for some days (service was added recently)
    if service == "license" and day_offset > 10:
        return None

    # All other services: mostly operational with occasional blips
    return random.random() > 0.02  # 98% uptime


if __name__ == "__main__":
    print("Generating fake historical data...")
    generate_data()
    print("Done! Historical data generated in the data/ directory.")
    print("\nEdge cases created:")
    print("  - www-homepage: Always operational (100% uptime)")
    print("  - telnet: Always down for first 5 days")
    print("  - portal: Degraded performance (90% uptime)")
    print("  - cso: Was down, recovered 7 days ago")
    print("  - gemini-chat: Intermittent issues (fails every 6 hours)")
    print("  - spring83: Recently went down (last 2 days)")
    print("  - astrobotany: Recent degradation (95% -> 85%)")
    print("  - license: Added recently (no data for older days)")
