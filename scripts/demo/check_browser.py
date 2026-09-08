#!/usr/bin/env python3
"""Run the local demo's reproducible desktop/mobile browser smoke checks.

Start ``python -m hidden_view_finder.server`` first, then run::

    .venv/bin/python scripts/demo/check_browser.py --url http://127.0.0.1:8000

Requires the optional Playwright Python package, its Chromium browser and the
browser's system libraries. This script installs nothing. Screenshots, the
downloaded scenario result and aggregate ``browser-checks.json`` are written to
``data/demo/browser`` by default. Use ``--skip-seoul`` for scenario-only checks.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from importlib.metadata import version
import json
from pathlib import Path
import time
from typing import Any


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--output-dir", type=Path, default=Path("data/demo/browser"))
    parser.add_argument("--skip-seoul", action="store_true", help="Do not exercise available real data")
    args = parser.parse_args()
    try:
        from playwright.sync_api import expect, sync_playwright
    except ImportError as error:
        raise SystemExit("Playwright is not installed. Follow docs/demo.md for optional browser checks.") from error

    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    checks: list[str] = []
    page_errors: list[str] = []
    console_errors: list[str] = []
    started = time.perf_counter()
    report: dict[str, Any] = {
        "url": args.url,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "playwright_version": version("playwright"),
        "desktop_viewport": {"width": 1440, "height": 1000},
        "mobile_viewport": {"width": 390, "height": 844},
        "checks": checks,
        "page_errors": page_errors,
        "console_errors": console_errors,
        "seoul": {"status": "not_run"},
    }

    def record(name: str) -> None:
        checks.append(name)

    def monitor(page: Any) -> None:
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)

    def submit(page: Any) -> None:
        with page.expect_response(lambda response: response.url.endswith("/api/recommend")) as pending:
            page.locator("#recommend-button").click()
        assert pending.value.ok, pending.value.text()
        expect(page.locator("#recommend-button")).to_be_enabled(timeout=30_000)
        expect(page.locator("#error-box")).to_be_hidden()

    def reset(page: Any) -> None:
        with page.expect_response(lambda response: response.url.endswith("/api/recommend")) as pending:
            page.locator("#reset-button").click()
        assert pending.value.ok, pending.value.text()
        expect(page.locator("#recommend-button")).to_be_enabled(timeout=30_000)
        expect(page.locator("#cards-list .spot-card")).to_have_count(3)
        expect(page.locator("#cards-list img")).to_have_count(3)

    def load_images(page: Any) -> None:
        images = page.locator("#cards-list img")
        expect(images).to_have_count(3)
        for image in images.all():
            image.scroll_into_view_if_needed()
            expect(image).to_be_visible()
            image.evaluate("(image) => image.decode()")
            assert image.evaluate("(image) => image.complete && image.naturalWidth > 0")
        captions = page.locator("#cards-list .image-caption")
        expect(captions).to_have_count(3)
        assert all("AI-generated anticipated view — actual scenery may differ." in caption.inner_text() for caption in captions.all())

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            report["browser_version"] = browser.version
            page = browser.new_page(viewport=report["desktop_viewport"], accept_downloads=True)
            monitor(page)
            response = page.goto(args.url, wait_until="networkidle")
            assert response and response.ok
            expect(page.locator("#cards-list .spot-card")).to_have_count(3)
            load_images(page)
            assert "undefined" not in page.locator("body").inner_text()
            assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
            record("desktop_default_three_recommendations_and_labeled_loaded_images")
            record("desktop_no_overflow_or_undefined")

            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(output / "ui-desktop.png"), full_page=True)
            page.screenshot(path=str(output / "ui-overview.png"), full_page=False)
            page.locator("#cards-list .spot-card").first.screenshot(path=str(output / "ui-recommendation-card.png"))

            page.get_by_role("button", name="비교표", exact=True).click()
            expect(page.locator("#comparison-panel")).to_be_visible()
            expect(page.locator("#comparison-panel tbody tr")).to_have_count(3)
            record("comparison_table_three_rows")
            page.get_by_role("button", name="추천의 근거", exact=True).click()
            expect(page.locator("#evidence-dialog")).to_be_visible()
            assert "근거 커버리지" in page.locator("#dialog-body").inner_text()
            page.get_by_role("button", name="닫기", exact=True).click()
            expect(page.locator("#evidence-dialog")).to_be_hidden()
            record("evidence_dialog_open_and_close")
            page.get_by_role("button", name="카드", exact=True).click()
            details = page.locator("#cards-list .card-details").first
            details.locator(":scope > summary").click()
            expect(details).to_have_attribute("open", "")
            assert "태양 위치" in details.inner_text()
            assert "미확인" in details.inner_text()
            record("card_details_criteria_uncertainties_and_solar_scope")

            with page.expect_download() as download:
                page.locator("#download-button").click()
            download.value.save_as(str(output / "scenario-result.json"))
            saved = json.loads((output / "scenario-result.json").read_text())
            assert saved["mode"] == "scenario" and len(saved["recommendations"]) == 3
            record("download_structured_json")

            page.locator("#max-travel").fill("1")
            submit(page)
            expect(page.locator("#empty-panel")).to_be_visible()
            expect(page.locator("#cards-list .spot-card")).to_have_count(0)
            expect(page.locator("#excluded-list .excluded-item")).to_have_count(10)
            record("hard_travel_constraint_empty_and_excluded_states")
            reset(page)
            record("reset_restores_default_and_preset_images")

            page.locator("#max-slope").fill("1")
            submit(page)
            expect(page.locator("#empty-panel")).to_be_visible()
            reset(page)
            expect(page.locator("#max-slope")).to_have_value("")
            record("slope_constraint_and_reset")

            page.locator("#crowd").select_option("any")
            submit(page)
            expect(page.locator("#cards-list img")).to_have_count(0)
            record("changed_request_does_not_reuse_exact_preset_images")
            reset(page)

            bootstrap_response = page.request.get(args.url.rstrip("/") + "/api/bootstrap")
            assert bootstrap_response.ok
            bootstrap = bootstrap_response.json()
            real_available = any(mode.get("id") == "seoul" and mode.get("available") for mode in bootstrap.get("modes", []))
            if real_available and not args.skip_seoul:
                page.locator("#mode").select_option("seoul")
                assert abs(float(page.locator("#lon").input_value()) - bootstrap["seoul_start"]["lon"]) < 1e-7
                submit(page)
                expect(page.locator("#cards-list .spot-card")).to_have_count(0)
                expect(page.locator("#empty-panel")).to_be_visible()
                expect(page.locator("#unverified-panel")).to_be_visible()
                initial = page.locator("#unverified-list .spot-card").count()
                if page.locator("#more-unverified").is_visible():
                    page.locator("#more-unverified").click()
                    assert page.locator("#unverified-list .spot-card").count() > initial
                assert page.locator("#map-stage polyline").count() > 0
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                report["seoul"] = {"status": "passed", "confirmed": 0, "unverified": page.locator("#unverified-list .spot-card").count(), "map_paths": page.locator("#map-stage polyline").count()}
                page.evaluate("window.scrollTo(0, 0)")
                page.screenshot(path=str(output / "ui-seoul.png"), full_page=False)
                record("seoul_real_paths_unverified_groups_and_load_more")
            else:
                report["seoul"] = {"status": "skipped", "reason": "explicit --skip-seoul" if args.skip_seoul else "prepared data unavailable"}

            mobile = browser.new_page(viewport=report["mobile_viewport"], is_mobile=True, has_touch=True)
            monitor(mobile)
            mobile.goto(args.url, wait_until="networkidle")
            expect(mobile.locator("#cards-list .spot-card")).to_have_count(3)
            load_images(mobile)
            assert not mobile.evaluate("document.documentElement.scrollWidth > innerWidth")
            mobile.evaluate("window.scrollTo(0, 0)")
            mobile.screenshot(path=str(output / "ui-mobile.png"), full_page=True)
            mobile.locator("#mobile-results").click()
            # Wait for smooth scrolling without an eval-based polling function;
            # the app deliberately disallows unsafe-eval in its CSP.
            expect(mobile.locator("#results-title")).to_be_in_viewport(timeout=5000)
            record("mobile_three_images_no_overflow_and_jump_to_results")
            assert not page_errors, page_errors
            assert not console_errors, console_errors
            record("no_page_or_console_errors")
            browser.close()
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        report["elapsed_s"] = round(time.perf_counter() - started, 3)
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["artifacts"] = sorted(path.name for path in output.iterdir() if path.is_file())
        (output / "browser-checks.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"status": report["status"], "checks": len(checks), "seoul": report["seoul"], "output_dir": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
