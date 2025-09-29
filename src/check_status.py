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
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from jinja2 import Environment, FileSystemLoader

ROOT_DIR = Path(__file__).parent.parent

# =============================================================================
# CONFIGURATION
# =============================================================================

HISTORY_FILE = ROOT_DIR / "data/history.jsonl"
OUTPUT_HTML = ROOT_DIR / "docs/index.html"
TEMPLATE_DIR = ROOT_DIR / "templates"

CHECK_TIMEOUT = 10.0  # seconds

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


@dataclass
class CheckResult:
    """Result of a single health check."""

    timestamp: str
    name: str
    success: bool
    response_time: float  # seconds


# =============================================================================
# HEALTH CHECK FUNCTIONS
# =============================================================================


@register(display_name="https://mozz.us")
async def check_mozz_us() -> bool:
    """Check https://mozz.us."""
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
        response = await client.get("https://mozz.us")
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


# =============================================================================
# HEALTH CHECK EXECUTION
# =============================================================================


async def run_single_check(name: str, func: Callable) -> CheckResult:
    """Run a single health check and measure response time."""
    start_time = time.perf_counter()
    try:
        success = await func()
    except Exception:
        success = False
    response_time = time.perf_counter() - start_time

    return CheckResult(
        timestamp=datetime.now(timezone.utc).isoformat(),
        name=name,
        success=success,
        response_time=response_time,
    )


async def run_all_checks() -> list[CheckResult]:
    """Run all registered health checks concurrently."""
    tasks = [run_single_check(check["name"], check["func"]) for check in _checks]
    return await asyncio.gather(*tasks)


def save_results(results: list[CheckResult]) -> None:
    """Append check results to the JSONL data file."""
    with HISTORY_FILE.open("a") as fp:
        for result in results:
            fp.write(json.dumps(asdict(result)) + "\n")


# =============================================================================
# HTML GENERATION
# =============================================================================


def load_check_data() -> list[dict[str, Any]]:
    """Load all check results from the JSONL file."""
    results = []
    with HISTORY_FILE.open() as fp:
        for line in fp:
            results.append(json.loads(line))
    return results


def calculate_service_stats(
    data: list[dict[str, Any]], service_name: str
) -> dict[str, Any]:
    """Calculate statistics for a single service over the past 30 days."""

    service_data = [d for d in data if d["name"] == service_name]

    if not service_data:
        return {
            "name": service_name,
            "current_status": "no-data",
            "current_status_text": "No Data",
            "uptime_days": [{"status": "no-data", "date": "", "uptime": 0}] * 30,
            "uptime_30d": 0,
            "avg_response_time": 0,
        }

    # Get most recent status
    most_recent = service_data[-1]
    current_status = "operational" if most_recent["success"] else "down"
    current_status_text = "Operational" if most_recent["success"] else "Down"

    # Calculate 30-day uptime (simplified - one bar per day)
    uptime_days = []
    for i in range(30):
        # For now, just show operational since we don't have historical data yet
        uptime_days.append({"status": "no-data", "date": f"Day {i + 1}", "uptime": 0})

    # Calculate overall uptime
    total_checks = len(service_data)
    successful_checks = sum(1 for d in service_data if d["success"])
    uptime_pct = (
        round((successful_checks / total_checks) * 100, 2) if total_checks > 0 else 0
    )

    # Calculate average response time
    avg_response = (
        round(sum(d["response_time"] for d in service_data) / total_checks * 1000, 2)
        if total_checks > 0
        else 0
    )

    return {
        "name": service_name,
        "current_status": current_status,
        "current_status_text": current_status_text,
        "uptime_days": uptime_days,
        "uptime_30d": uptime_pct,
        "avg_response_time": avg_response,
    }


def generate_html() -> None:
    """Generate the status page HTML from historical data."""
    data = load_check_data()

    # Get unique service names
    service_names = list({check["name"] for check in _checks})

    # Calculate stats for each service
    services = [calculate_service_stats(data, name) for name in service_names]

    # Determine overall status
    if not services:
        overall_status = "operational"
        status_message = "No services configured"
    elif all(s["current_status"] == "operational" for s in services):
        overall_status = "operational"
        status_message = "All systems operational"
    elif any(s["current_status"] == "down" for s in services):
        overall_status = "down"
        status_message = "Some systems are experiencing issues"
    else:
        overall_status = "degraded"
        status_message = "System performance degraded"

    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR))
    template = env.get_template("index.html")
    html_content = template.render(
        overall_status=overall_status,
        status_message=status_message,
        services=services,
        last_updated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    )

    OUTPUT_HTML.write_text(html_content)


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
        results = await run_all_checks()
        save_results(results)
        for result in results:
            status = "✓" if result.success else "✗"
            print(f"{status} {result.name} ({result.response_time * 1000:.0f}ms)")

    if args.generate:
        print("Generating HTML status page...")
        generate_html()
        print(f"Status page generated: {OUTPUT_HTML}")


if __name__ == "__main__":
    asyncio.run(main())
