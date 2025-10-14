#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx", "jinja2"]
# ///

"""Automated health checker and status page generator."""

import argparse
import asyncio
import csv
import ssl
import traceback
from collections.abc import Callable
from dataclasses import dataclass
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


def register(
    name: str,
    display_name: str | None = None,
    url: str | None = None,
    group: str | None = None,
):
    """Decorator to register a health check function.

    Args:
        name: Unique identifier for the health check
        display_name: Human-readable name shown on the status page (defaults to name)
        url: Optional URL to link to from the status page
        group: Optional group for organizing services on the status page.
    """

    def decorator(func: Callable) -> Callable:
        _checks.append(
            {
                "name": name,
                "display_name": display_name or name,
                "url": url,
                "group": group,
                "func": func,
            }
        )
        return func

    return decorator


# =============================================================================
# DATA MODELS
# =============================================================================


class Status(str, Enum):
    """Status of a service."""

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
class ServiceData:
    """Data for a service."""

    name: str
    display_name: str
    url: str | None
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


@dataclass
class ServiceGroup:
    """A group of services."""

    name: str | None
    services: list[ServiceData]


# TODO: Move portal, astrobotany, and hn-gopher to top
# TODO: Make group names look like h2 with blue background

# =============================================================================
# HEALTH CHECK FUNCTIONS
# =============================================================================


async def fetch_tcp(
    host: str,
    port: int,
    request: bytes = b"",
    read_until: bytes | None = None,
) -> bytes:
    """Fetch data from a TCP server by sending a request and reading response.

    Args:
        host: The server hostname
        port: The server port
        request: The request bytes to send (default empty)
        read_until: If specified, read until this byte sequence is found.
                   If None, read until connection closes.

    Returns:
        The response bytes from the server
    """
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port),
        timeout=CHECK_TIMEOUT,
    )

    writer.write(request)
    await writer.drain()

    if read_until:
        future = reader.readuntil(read_until)
    else:
        future = reader.read()
    response = await asyncio.wait_for(future, timeout=CHECK_TIMEOUT)

    writer.close()
    await writer.wait_closed()

    return response


async def fetch_gemini(host: str, port: int, url: str) -> bytes:
    """Fetch data from a Gemini server over TLS without certificate verification.

    Args:
        host: The server hostname
        port: The server port
        url: The full URL to request (e.g., "gemini://mozz.us/")

    Returns:
        The response header line from the server
    """
    # Create SSL context that doesn't verify certificates
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=context),
        timeout=CHECK_TIMEOUT,
    )

    # Send Gemini request: url\r\n
    request = f"{url}\r\n".encode()
    writer.write(request)
    await writer.drain()

    # Read response header
    response = await asyncio.wait_for(reader.readline(), timeout=CHECK_TIMEOUT)

    writer.close()
    await writer.wait_closed()

    return response


@register(
    name="astrobotany",
    display_name="Astrobotany",
    url="gemini://astrobotany.mozz.us",
)
async def check_gemini_astrobotany() -> bool:
    response = await fetch_gemini(
        "astrobotany.mozz.us", 1965, "gemini://astrobotany.mozz.us/"
    )
    return response.startswith(b"20 text/gemini\r\n")


@register(
    name="portal",
    display_name="Smolnet Portal",
    url="https://portal.mozz.us",
)
async def check_portal_mozz_us() -> bool:
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
        response = await client.get("https://portal.mozz.us")
        return response.status_code == 200


@register(
    name="hngopher",
    display_name="HN Gopher",
    url="gopher://hngopher.com",
)
async def check_gopher_hngopher() -> bool:
    response = await fetch_tcp("hngopher.com", 70, b"\r\n")
    return len(response) > 0


@register(
    name="www-homepage",
    display_name="Homepage",
    url="https://mozz.us",
    group="WWW",
)
async def check_mozz_us() -> bool:
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
        response = await client.get("https://mozz.us")
        return response.status_code == 200


@register(
    name="ascii-gallery",
    display_name="ASCII Art Gallery",
    url="https://ascii.mozz.us",
    group="WWW",
)
async def check_ascii_mozz_us() -> bool:
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
        response = await client.get("https://ascii.mozz.us")
        return response.status_code == 200


@register(
    name="git",
    display_name="Git Mirror",
    url="https://git.mozz.us",
    group="WWW",
)
async def check_git_mozz_us() -> bool:
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
        response = await client.get("https://git.mozz.us")
        return response.status_code == 200


@register(
    name="emporium",
    display_name="ASCII Art Emporium",
    url="https://ascii.mozz.us:7070",
    group="WWW",
)
async def check_ascii_mozz_us_7070() -> bool:
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
        response = await client.get("https://ascii.mozz.us:7070")
        return response.status_code == 200


@register(
    name="spring83",
    display_name="Spring '83",
    url="https://spring83.mozz.us",
    group="WWW",
)
async def check_spring83() -> bool:
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
        response = await client.get("https://spring83.mozz.us")
        return response.status_code == 200


@register(
    name="shiftjis-art",
    display_name="ShiftJIS Art",
    url="https://aa.mozz.us",
    group="WWW",
)
async def check_shiftjis_art() -> bool:
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
        response = await client.get("https://aa.mozz.us")
        return response.status_code == 200


@register(
    name="goodvibes",
    display_name="Good Vibes Flash",
    url="https://goodvibes.mozz.us",
    group="WWW",
)
async def check_goodvibes() -> bool:
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
        response = await client.get("https://goodvibes.mozz.us")
        return response.status_code == 302


@register(
    name="license",
    display_name="Human Software License",
    url="https://license.mozz.us",
    group="WWW",
)
async def check_hsl() -> bool:
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
        response = await client.get("https://license.mozz.us")
        return response.status_code == 200


@register(
    name="gopher-homepage",
    display_name="Gopher Homepage",
    url="gopher://mozz.us",
    group="Gopher",
)
async def check_gopher_mozz_us() -> bool:
    response = await fetch_tcp("mozz.us", 70, b"\r\n")
    return len(response) > 0


@register(
    name="cocktails",
    display_name="Cocktail Database",
    url="gopher://mozz.us:7003",
    group="Gopher",
)
async def check_gopher_mozz_us_7003() -> bool:
    response = await fetch_tcp("mozz.us", 7003, b"\r\n")
    return len(response) > 0


@register(
    name="flask-gopher",
    display_name="Flask-Gopher",
    url="gopher://mozz.us:7005",
    group="Gopher",
)
async def check_gopher_mozz_us_7005() -> bool:
    response = await fetch_tcp("mozz.us", 7005, b"\r\n")
    return len(response) > 0


@register(
    name="gopher-z",
    display_name="Gopher-Z",
    url="gopher://mozz.us:7006",
    group="Gopher",
)
async def check_gopher_mozz_us_7006() -> bool:
    response = await fetch_tcp("mozz.us", 7006, b"\r\n")
    return len(response) > 0


@register(
    name="nex-homepage",
    display_name="Nex Homepage",
    url="nex://mozz.us",
    group="Nex",
)
async def check_nex_mozz_us() -> bool:
    response = await fetch_tcp("mozz.us", 1900, b"\r\n")
    return b"ride the wave" in response


@register(
    name="finger",
    display_name="Finger Directory",
    url="finger://mozz.us/michael",
    group="Finger",
)
async def check_finger_mozz_us() -> bool:
    response = await fetch_tcp("mozz.us", 79, b"michael\r\n")
    return b"michael@mozz.us" in response


@register(
    name="spartan-homepage",
    display_name="Spartan Homepage",
    url="spartan://mozz.us",
    group="Spartan",
)
async def check_spartan_mozz_us() -> bool:
    response = await fetch_tcp("mozz.us", 300, b"mozz.us / 0\r\n")
    return response.startswith(b"2 text/gemini\r\n")


@register(
    name="gemini-homepage",
    display_name="Gemini Homepage",
    url="gemini://mozz.us",
    group="Gemini",
)
async def check_gemini_mozz_us() -> bool:
    response = await fetch_gemini("mozz.us", 1965, "gemini://mozz.us/")
    return response.startswith(b"20 text/gemini;")


@register(
    name="gemini-chat",
    display_name="Gemini Chat",
    url="gemini://chat.mozz.us",
    group="Gemini",
)
async def check_gemini_chat() -> bool:
    response = await fetch_gemini("chat.mozz.us", 1965, "gemini://chat.mozz.us/")
    return response.startswith(b"20 text/gemini\r\n")


# @register(
#     name="cso",
#     display_name="CCSO Nameserver",
#     url="cso://mozz.us",
#     group="CSO",
# )
# async def check_cso() -> bool:
#     response = await fetch_tcp("mozz.us", 105, b"\r\nstatus\r\n", read_until=b"\r\n")
#     return len(response) > 0


# TODO: telnet://
# TODO: fix cso://


# =============================================================================
# HEALTH CHECK EXECUTION
# =============================================================================


async def run_single_check(name: str, func: Callable, timestamp: int) -> CheckResult:
    """Run a single health check."""
    try:
        success = await func()
    except TimeoutError:
        print(f"Timeout checking {name}")
        success = False
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
    """Append check results to date-bucketed CSV file."""
    if not results:
        return

    # Convert timestamp to datetime to get the date
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    date_str = dt.strftime("%Y%m%d")

    # Create history file path for this date
    history_file = HISTORY_DIR / f"history-{date_str}.csv"

    # Ensure directory exists
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)

    # Append each result as a CSV line: timestamp,name,success
    with history_file.open("a", newline="") as fp:
        writer = csv.writer(fp)
        for result in results:
            success_str = "Y" if result.success else "N"
            writer.writerow([result.timestamp, result.name, success_str])


# =============================================================================
# HTML GENERATION
# =============================================================================


def load_check_results() -> list[CheckResult]:
    """Load all check results from date-bucketed CSV files."""
    results = []
    now = datetime.now(timezone.utc)

    # Load data from the configured number of days
    for i in range(HISTORY_TICKS):
        day = now - timedelta(days=i)
        date_str = day.strftime("%Y%m%d")
        history_file = HISTORY_DIR / f"history-{date_str}.csv"

        if not history_file.exists():
            continue

        with history_file.open(newline="") as fp:
            reader = csv.reader(fp)
            for row in reader:
                timestamp = int(row[0])
                name = row[1]
                success = row[2] == "Y"
                result = CheckResult(timestamp=timestamp, name=name, success=success)
                results.append(result)

    return results


def calculate_all_service_stats(results: list[CheckResult]) -> list[ServiceData]:
    """Calculate statistics for all services in a single pass."""

    today = datetime.today()

    service_map: dict[str, ServiceData] = {}
    for check in _checks:
        ticks = {}
        for day_offset in range(HISTORY_TICKS - 1, -1, -1):  # noqa
            tick_date = (today - timedelta(days=day_offset)).date()
            ticks[tick_date] = UptimeTick(date=tick_date, results=[])

        service_map[check["name"]] = ServiceData(
            name=check["name"],
            display_name=check["display_name"],
            url=check["url"],
            ticks=ticks,
        )

    for result in results:
        service = service_map.get(result.name)
        if not service:
            continue

        timestamp = datetime.fromtimestamp(result.timestamp, tz=timezone.utc)
        result_date = timestamp.date()

        tick = service.ticks.get(result_date)
        if not tick:
            continue

        tick.results.append(result.success)

    return list(service_map.values())


def generate_html() -> None:
    """Generate the status page HTML from historical data."""
    data = load_check_results()
    services = calculate_all_service_stats(data)

    # Group services by their group name
    groups_dict: dict[str | None, list[ServiceData]] = {}
    check_groups = {check["name"]: check["group"] for check in _checks}

    for service in services:
        group_name = check_groups.get(service.name)
        if group_name not in groups_dict:
            groups_dict[group_name] = []
        groups_dict[group_name].append(service)

    # Convert to list of ServiceGroup objects
    groups = [ServiceGroup(name, services) for name, services in groups_dict.items()]

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
        groups=groups,
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
