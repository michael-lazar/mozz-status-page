#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx", "jinja2"]
# ///

"""Automated health checker and status page generator."""

import argparse
import asyncio
import json
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone, date
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
from jinja2 import Template

ROOT_DIR = Path(__file__).parent.parent

# =============================================================================
# CONFIGURATION
# =============================================================================

HISTORY_DIR = ROOT_DIR / "data"
OUTPUT_FILE = ROOT_DIR / "docs/index.html"
TEMPLATE_FILE = ROOT_DIR / "templates/index.html"

CHECK_TIMEOUT = 10.0  # seconds
HISTORY_TICKS = 30  # days

# =============================================================================
# HEALTH CHECK REGISTRY
# =============================================================================

_checks: list[dict[str, Any]] = []


def register(display_name: str):
    """Decorator to register a health check function."""

    def decorator(func: Callable) -> Callable:
        _checks.append({"name": display_name, "func": func})
        return func

    return decorator


# =============================================================================
# DATA MODELS
# =============================================================================


class Status(str, Enum):
    OPERATIONAL = "operational"
    DEGRADED = "degraded"
    DOWN = "down"
    NO_DATA = "no-data"


@dataclass
class CheckResult:
    """Result of a single health check."""

    name: str
    success: bool
    timestamp: int  # epoch seconds


@dataclass
class UptimeTick:
    """Uptime data for a single time period."""

    date: date
    results: list[bool]

    @property
    def current_status(self) -> Status:
        if not self.results:
            return Status.NO_DATA

        if self.results[-1]:
            return Status.OPERATIONAL
        else:
            return Status.DOWN

    @property
    def status(self) -> Status:
        if not self.results:
            return Status.NO_DATA

        match self.uptime:
            case 100:
                return Status.OPERATIONAL
            case 0:
                return Status.DOWN
            case _:
                return Status.DEGRADED

    @property
    def uptime(self) -> float:
        if not self.results:
            return 0

        successes = sum(int(r) for r in self.results)
        uptime_pct = (successes / len(self.results)) * 100
        return uptime_pct


@dataclass
class ServiceStats:
    """Statistics for a service."""

    name: str
    ticks: dict[date, UptimeTick]

    @property
    def current_status(self) -> Status:
        if not self.ticks:
            return Status.NO_DATA

        return self.ticks[max(self.ticks)].current_status

    @property
    def current_status_text(self) -> str:
        """Get human-readable status text."""
        match self.current_status:
            case Status.OPERATIONAL:
                return "Operational"
            case Status.DEGRADED:
                return "Degraded"
            case Status.DOWN:
                return "Down"
            case Status.NO_DATA:
                return "No Data"
            case _:
                return "Unknown"


# =============================================================================
# HEALTH CHECK FUNCTIONS
# =============================================================================


@register(display_name="https://mozz.us")
async def check_mozz_us() -> bool:
    """Check https://mozz.us."""
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
        response = await client.get("https://mozz.usdadadasdsa")
        return response.status_code == 200


@register(display_name="https://ascii.mozz.us")
async def check_ascii_mozz_us() -> bool:
    """Check https://ascii.mozz.us."""
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
        response = await client.get("https://ascii.mozz.us")
        return response.status_code == 200


@register(display_name="https://portal.mozz.us")
async def check_portal_mozz_us() -> bool:
    """Check https://portal.mozz.us."""
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
        response = await client.get("https://portal.mozz.us")
        return response.status_code == 200


@register(display_name="https://git.mozz.us")
async def check_git_mozz_us() -> bool:
    """Check https://git.mozz.us."""
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
        response = await client.get("https://git.mozz.us")
        return response.status_code == 200


@register(display_name="https://ascii.mozz.us:7070")
async def check_ascii_mozz_us_7070() -> bool:
    """Check https://ascii.mozz.us:7070."""
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
        response = await client.get("https://ascii.mozz.us:7070")
        return response.status_code == 200


# TODO: Add more health checks
# TODO: Add health check groups by protocol
# TODO: Make it more compact

# =============================================================================
# HEALTH CHECK EXECUTION
# =============================================================================


async def run_single_check(name: str, func: Callable, timestamp: int) -> CheckResult:
    """Run a single health check."""
    try:
        success = await func()
    except Exception:
        print(f"Error checking {name}:")
        traceback.print_exc()
        success = False

    return CheckResult(name=name, success=success, timestamp=timestamp)


async def run_all_checks(timestamp: int) -> list[CheckResult]:
    """Run all registered health checks concurrently."""
    tasks = [
        run_single_check(check["name"], check["func"], timestamp) for check in _checks
    ]
    return await asyncio.gather(*tasks)


def save_check_results(results: list[CheckResult], timestamp: int) -> None:
    """Append check results to date-bucketed JSONL file."""
    if not results:
        return

    # Convert timestamp to datetime to get the date
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    date_str = dt.strftime("%Y%m%d")

    # Create history file path for this date
    history_file = HISTORY_DIR / f"history-{date_str}.jsonl"

    # Ensure directory exists
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    # Append each result as a separate line
    with history_file.open("a") as fp:
        for result in results:
            fp.write(json.dumps(asdict(result)) + "\n")


# =============================================================================
# HTML GENERATION
# =============================================================================


def load_check_results() -> list[CheckResult]:
    """Load all check results from date-bucketed JSONL files."""
    results = []
    now = datetime.now(timezone.utc)

    # Load data from the configured number of days
    for i in range(HISTORY_TICKS):
        day = now - timedelta(days=i)
        date_str = day.strftime("%Y%m%d")
        history_file = HISTORY_DIR / f"history-{date_str}.jsonl"

        if not history_file.exists():
            continue

        with history_file.open() as fp:
            for line in fp:
                data = json.loads(line)
                result = CheckResult(**data)
                results.append(result)

    return results


def calculate_all_service_stats(results: list[CheckResult]) -> list[ServiceStats]:
    """Calculate statistics for all services in a single pass."""

    today = datetime.today()

    stats_map: dict[str, ServiceStats] = {}

    for check in _checks:
        ticks = {}
        for day_offset in range(HISTORY_TICKS - 1, -1, -1):  # noqa
            tick_date = (today - timedelta(days=day_offset)).date()
            ticks[tick_date] = UptimeTick(date=tick_date, results=[])

        stats_map[check["name"]] = ServiceStats(name=check["name"], ticks=ticks)

    for result in results:
        service_stats = stats_map.get(result.name)
        if not service_stats:
            continue

        timestamp = datetime.fromtimestamp(result.timestamp, tz=timezone.utc)
        result_date = timestamp.date()

        tick = service_stats.ticks.get(result_date)
        if not tick:
            continue

        tick.results.append(result.success)

    return list(stats_map.values())


def generate_html() -> None:
    """Generate the status page HTML from historical data."""
    data = load_check_results()
    services = calculate_all_service_stats(data)

    # Determine overall status
    if not services:
        overall_status = Status.OPERATIONAL
        status_message = "No services configured"
    elif all(s.current_status == Status.OPERATIONAL for s in services):
        overall_status = Status.OPERATIONAL
        status_message = "All systems operational"
    elif any(s.current_status == Status.DOWN for s in services):
        overall_status = Status.DOWN
        status_message = "Some systems are experiencing issues"
    else:
        overall_status = Status.DEGRADED
        status_message = "System performance degraded"

    last_updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Generate HTML with all data rendered server-side
    template_content = TEMPLATE_FILE.read_text()
    template = Template(template_content)
    html_content = template.render(
        overall_status=overall_status,
        status_message=status_message,
        last_updated=last_updated,
        services=services,
    )

    OUTPUT_FILE.write_text(html_content)


# =============================================================================
# MAIN
# =============================================================================


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Health checker and status page generator"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run health checks",
    )
    parser.add_argument(
        "--generate",
        action="store_true",
        help="Generate HTML status page",
    )

    args = parser.parse_args()

    # Default to doing both if no args specified
    if not args.check and not args.generate:
        args.check = True
        args.generate = True

    if args.check:
        print("Running health checks...")
        timestamp = int(datetime.now(timezone.utc).timestamp())
        results = await run_all_checks(timestamp)
        save_check_results(results, timestamp)
        for result in results:
            status = "✓" if result.success else "✗"
            print(f"{status} {result.name}")

    if args.generate:
        print("Generating HTML status page...")
        generate_html()
        print(f"Status page generated: {OUTPUT_FILE}")


if __name__ == "__main__":
    asyncio.run(main())
