#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx", "jinja2", "markdown"]
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
from functools import cached_property

import httpx
import markdown
from jinja2 import Template

ROOT_DIR = Path(__file__).parent.parent

# =============================================================================
# CONFIGURATION
# =============================================================================

DATA_DIR = ROOT_DIR / "data"
DIST_DIR = ROOT_DIR / "dist"
TEMPLATE_DIR = ROOT_DIR / "templates"
INCIDENTS_FILE = ROOT_DIR / "INCIDENTS.md"

CHECK_TIMEOUT = 10.0  # seconds
HISTORY_TICKS = 45  # days

# =============================================================================
# HEALTH CHECK REGISTRY
# =============================================================================

_checks: list[dict[str, Any]] = []


def register(
    name: str,
    title: str | None = None,
    url: str | None = None,
    group: str | None = None,
):
    """Decorator to register a health check function.

    Args:
        name: Unique identifier for the health check
        title: Human-readable name shown on the status page (defaults to name)
        url: Optional URL to link to from the status page
        group: Optional group for organizing services on the status page.
    """

    def decorator(func: Callable) -> Callable:
        _checks.append(
            {
                "name": name,
                "title": title or name,
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


class ServiceStatus(str, Enum):
    """Overall status of a service (current state)."""

    OPERATIONAL = "operational"
    DOWN = "down"


class TickStatus(str, Enum):
    """Status of an uptime tick (aggregated over a time period)."""

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

    @cached_property
    def status(self) -> TickStatus:
        """Get the aggregated status for the entire tick period."""
        if not self.results:
            return TickStatus.NO_DATA

        match self.uptime:
            case 100:
                return TickStatus.OPERATIONAL
            case 0:
                return TickStatus.DOWN
            case _:
                return TickStatus.DEGRADED

    @cached_property
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
    title: str
    url: str | None
    ticks: dict[date, UptimeTick]

    @cached_property
    def status(self) -> ServiceStatus:
        if not self.ticks:
            return ServiceStatus.OPERATIONAL

        last_tick = self.ticks[max(self.ticks)]
        if not last_tick.results:
            return ServiceStatus.OPERATIONAL

        if last_tick.results[-1]:
            return ServiceStatus.OPERATIONAL
        else:
            return ServiceStatus.DOWN

    @cached_property
    def status_text(self) -> str:
        """Get human-readable status text."""
        match self.status:
            case ServiceStatus.OPERATIONAL:
                return "Operational"
            case ServiceStatus.DOWN:
                return "Down"
            case _:
                raise ValueError()

    @cached_property
    def cumulative_uptime(self) -> float:
        """Calculate cumulative uptime percentage across all ticks."""
        all_results = []
        for tick in self.ticks.values():
            all_results.extend(tick.results)

        if not all_results:
            return 100.0

        successes = sum(int(r) for r in all_results)
        uptime_pct = (successes / len(all_results)) * 100
        return uptime_pct


@dataclass
class ServiceGroup:
    """A group of services."""

    name: str | None
    services: list[ServiceData]
    slug: str | None = None


@dataclass
class Incident:
    """An incident with date and content."""

    date: str
    content_html: str


# =============================================================================
# HEALTH CHECK FUNCTIONS
# =============================================================================


async def fetch_tcp(
    host: str, port: int, request: bytes = b"", read_until: bytes | None = None
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


async def fetch_www(url: str) -> httpx.Response:
    """Fetch an HTTP(S) URL."""
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
        return await client.get(url)


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


@register("astrobotany", title="Astrobotany", url="gemini://astrobotany.mozz.us")
async def check_gemini_astrobotany() -> bool:
    response = await fetch_gemini("astrobotany.mozz.us", 1965, "gemini://astrobotany.mozz.us/")
    return response.startswith(b"20 text/gemini\r\n")


@register("portal", title="Smolnet Portal", url="https://portal.mozz.us")
async def check_portal_mozz_us() -> bool:
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
        response = await client.get("https://portal.mozz.us")
        return response.status_code == 200


@register("hngopher", title="HN Gopher", url="gopher://hngopher.com")
async def check_gopher_hngopher() -> bool:
    response = await fetch_tcp("hngopher.com", 70, b"\r\n")
    return len(response) > 0


@register("www-homepage", title="Homepage", url="https://mozz.us", group="WWW")
async def check_mozz_us() -> bool:
    response = await fetch_www("https://mozz.us")
    return response.status_code == 200


@register("ascii-gallery", title="ASCII Art Gallery", url="https://ascii.mozz.us", group="WWW")
async def check_ascii_mozz_us() -> bool:
    response = await fetch_www("https://ascii.mozz.us")
    return response.status_code == 200


@register("git", title="Git Mirror", url="https://git.mozz.us", group="WWW")
async def check_git_mozz_us() -> bool:
    response = await fetch_www("https://git.mozz.us")
    return response.status_code == 200


@register("emporium", title="ASCII Art Emporium", url="https://ascii.mozz.us:7070", group="WWW")
async def check_ascii_mozz_us_7070() -> bool:
    response = await fetch_www("https://ascii.mozz.us:7070")
    return response.status_code == 200


@register("spring83", title="Spring '83", url="https://spring83.mozz.us", group="WWW")
async def check_spring83() -> bool:
    response = await fetch_www("https://spring83.mozz.us")
    return response.status_code == 200


@register("shiftjis-art", title="ShiftJIS Art", url="https://aa.mozz.us", group="WWW")
async def check_shiftjis_art() -> bool:
    response = await fetch_www("https://aa.mozz.us")
    return response.status_code == 200


@register("goodvibes", title="Good Vibes", url="https://goodvibes.mozz.us", group="WWW")
async def check_goodvibes() -> bool:
    response = await fetch_www("https://goodvibes.mozz.us")
    return response.status_code == 302


@register("license", title="Human Software License", url="https://license.mozz.us", group="WWW")
async def check_hsl() -> bool:
    response = await fetch_www("https://license.mozz.us")
    return response.status_code == 200


@register("slashdot", title="Slashdot Mirror", url="https://slashdot.mozz.us", group="WWW")
async def check_slashdot() -> bool:
    response = await fetch_www("https://slashdot.mozz.us")
    return response.status_code == 200


@register("gopher-homepage", title="Gopher Homepage", url="gopher://mozz.us", group="Gopher")
async def check_gopher_mozz_us() -> bool:
    response = await fetch_tcp("mozz.us", 70, b"\r\n")
    return len(response) > 0


@register("cocktails", title="Cocktail Database", url="gopher://mozz.us:7003", group="Gopher")
async def check_gopher_mozz_us_7003() -> bool:
    response = await fetch_tcp("mozz.us", 7003, b"\r\n")
    return len(response) > 0


@register("flask-gopher", title="Flask-Gopher", url="gopher://mozz.us:7005", group="Gopher")
async def check_gopher_mozz_us_7005() -> bool:
    response = await fetch_tcp("mozz.us", 7005, b"\r\n")
    return len(response) > 0


@register("gopher-z", title="Gopher-Z", url="gopher://mozz.us:7006", group="Gopher")
async def check_gopher_mozz_us_7006() -> bool:
    response = await fetch_tcp("mozz.us", 7006, b"\r\n")
    return len(response) > 0


@register("nex-homepage", title="Nex Homepage", url="nex://mozz.us", group="Nex")
async def check_nex_mozz_us() -> bool:
    response = await fetch_tcp("mozz.us", 1900, b"\r\n")
    return b"ride the wave" in response


@register("finger", title="Finger Directory", url="finger://mozz.us/michael", group="Finger")
async def check_finger_mozz_us() -> bool:
    response = await fetch_tcp("mozz.us", 79, b"michael\r\n")
    return b"michael@mozz.us" in response


@register("spartan-homepage", title="Spartan Homepage", url="spartan://mozz.us", group="Spartan")
async def check_spartan_mozz_us() -> bool:
    response = await fetch_tcp("mozz.us", 300, b"mozz.us / 0\r\n")
    return response.startswith(b"2 text/gemini\r\n")


@register("gemini-homepage", title="Gemini Homepage", url="gemini://mozz.us", group="Gemini")
async def check_gemini_mozz_us() -> bool:
    response = await fetch_gemini("mozz.us", 1965, "gemini://mozz.us/")
    return response.startswith(b"20 text/gemini;")


@register("gemini-chat", title="Gemini Chat", url="gemini://chat.mozz.us", group="Gemini")
async def check_gemini_chat() -> bool:
    response = await fetch_gemini("chat.mozz.us", 1965, "gemini://chat.mozz.us/")
    return response.startswith(b"20 text/gemini\r\n")


@register(name="cso", title="CCSO Nameserver", url="cso://mozz.us", group="CSO")
async def check_cso() -> bool:
    expected = b"200:Database ready."
    response = await fetch_tcp("mozz.us", 105, b"status\r\n", read_until=expected)
    return response == expected


@register(name="telnet", title="Telnet Server", url="telnet://mozz.us", group="Telnet")
async def check_telnet() -> bool:
    # Telnet server sends negotiation data immediately upon connection
    # Read a small amount to verify the server is responding
    response = await fetch_tcp("mozz.us", 23, b"", read_until=b"\xff")
    return len(response) > 0


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
    tasks = [run_single_check(check["name"], check["func"], timestamp) for check in _checks]
    return await asyncio.gather(*tasks)


def save_check_results(results: list[CheckResult], timestamp: int) -> None:
    """Append check results to date-bucketed CSV file."""
    if not results:
        return

    # Convert timestamp to datetime to get the date
    dt = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    year = dt.strftime("%Y")
    date_str = dt.strftime("%Y%m%d")

    # Create history file path: data/2025/checks-20250114.csv
    year_dir = DATA_DIR / year
    history_file = year_dir / f"checks-{date_str}.csv"

    # Ensure directory exists
    year_dir.mkdir(parents=True, exist_ok=True)

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
        year = day.strftime("%Y")
        date_str = day.strftime("%Y%m%d")

        # Load from: data/2025/checks-20250114.csv
        year_dir = DATA_DIR / year
        history_file = year_dir / f"checks-{date_str}.csv"

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
            title=check["title"],
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


def parse_incidents() -> list[Incident]:
    """Parse the INCIDENTS.md file and extract ongoing incidents."""
    if not INCIDENTS_FILE.exists():
        return []

    content = INCIDENTS_FILE.read_text()
    lines = content.splitlines()

    incidents: list[Incident] = []
    in_ongoing_section = False
    current_incident_date = None
    current_incident_content: list[str] = []

    def flush() -> None:
        if current_incident_date and current_incident_content:
            content_md = "\n".join(current_incident_content).strip()
            content_html = markdown.markdown(content_md)
            incident = Incident(date=current_incident_date, content_html=content_html)
            incidents.append(incident)

    for line in lines:
        if line.startswith("## "):
            if in_ongoing_section:
                # Stop when we hit the second ## header (Resolved)
                break
            else:
                in_ongoing_section = True
                continue

        if in_ongoing_section:
            # Check for H3 (incident date)
            if line.startswith("### "):
                # Save previous incident if exists
                flush()
                # Start new incident
                current_incident_date = line[4:].strip()  # Remove "### "
                current_incident_content = []
            else:
                # Accumulate content for current incident
                current_incident_content.append(line)

    # Save the last incident
    flush()

    return incidents


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
    groups = [
        ServiceGroup(
            name=name,
            services=services,
            slug=name.lower().replace(" ", "-") if name else None,
        )
        for name, services in groups_dict.items()
    ]

    # Determine overall status
    if any(s.status == ServiceStatus.DOWN for s in services):
        overall_status = ServiceStatus.DOWN
        status_message = "Some systems are experiencing issues"
    else:
        overall_status = ServiceStatus.OPERATIONAL
        status_message = "All systems operational"

    last_updated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

    # Parse incidents
    incidents = parse_incidents()

    # Generate HTML with all data rendered server-side
    template_file = TEMPLATE_DIR / "index.html"
    template_content = template_file.read_text()
    template = Template(template_content)
    html_content = template.render(
        overall_status=overall_status,
        status_message=status_message,
        last_updated=last_updated,
        groups=groups,
        incidents=incidents,
        history_ticks=HISTORY_TICKS,
    )

    # Ensure output directory exists
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    output_file = DIST_DIR / "index.html"
    output_file.write_text(html_content)


# =============================================================================
# MAIN
# =============================================================================


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Health checker and status page generator")
    parser.add_argument("--check", action="store_true", help="Run health checks")
    parser.add_argument("--build", action="store_true", help="Rebuild HTML status page")
    args = parser.parse_args()

    if args.check:
        print("Running health checks...")
        timestamp = int(datetime.now(timezone.utc).timestamp())
        results = await run_all_checks(timestamp)
        save_check_results(results, timestamp)
        for result in results:
            status = "✓" if result.success else "✗"
            print(f"{status} {result.name}")

    if args.build:
        print("Generating HTML status page...")
        generate_html()
        print("Status page generated.")


if __name__ == "__main__":
    asyncio.run(main())
