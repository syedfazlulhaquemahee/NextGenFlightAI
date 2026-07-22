"""Capture B2C user-manual screenshots from the local Skairova site.

This script talks directly to Safari's WebDriver endpoint so the project does
not need Selenium or Playwright as an extra dependency.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path


BASE_URL = "http://127.0.0.1:5001"
WEBDRIVER_URL = "http://127.0.0.1:4444"
SCREENSHOT_DIR = Path("docs/screenshots")


class SafariDriver:
    def __init__(self) -> None:
        self.session_id = ""

    def request(self, method: str, path: str, payload: dict | None = None, timeout: int = 30):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{WEBDRIVER_URL}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw) if raw else {"value": None}

    def start(self) -> None:
        response = self.request(
            "POST",
            "/session",
            {"capabilities": {"alwaysMatch": {"browserName": "safari"}}},
            timeout=20,
        )
        self.session_id = response["value"]["sessionId"]
        self.request(
            "POST",
            f"/session/{self.session_id}/window/rect",
            {"x": 40, "y": 40, "width": 1440, "height": 1050},
        )

    def stop(self) -> None:
        if self.session_id:
            try:
                self.request("DELETE", f"/session/{self.session_id}")
            except Exception:
                pass

    def navigate(self, url: str, delay: float = 1.4) -> None:
        self.request("POST", f"/session/{self.session_id}/url", {"url": url})
        self.wait_ready()
        time.sleep(delay)

    def wait_ready(self, timeout: float = 20) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                state = self.js("return document.readyState")
                if state == "complete":
                    return
            except Exception:
                pass
            time.sleep(0.25)

    def wait_for(self, script: str, timeout: float = 30) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.js(script):
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False

    def js(self, script: str):
        response = self.request(
            "POST",
            f"/session/{self.session_id}/execute/sync",
            {"script": script, "args": []},
            timeout=30,
        )
        return response.get("value")

    def screenshot(self, name: str) -> None:
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        response = self.request("GET", f"/session/{self.session_id}/screenshot", timeout=30)
        image = base64.b64decode(response["value"])
        (SCREENSHOT_DIR / name).write_bytes(image)
        print(f"captured {SCREENSHOT_DIR / name}")


def safe_capture(driver: SafariDriver, name: str, url: str, setup_script: str | None = None) -> None:
    try:
        driver.navigate(url)
        if setup_script:
            driver.js(setup_script)
            time.sleep(0.8)
        driver.screenshot(name)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, RuntimeError) as exc:
        print(f"skipped {name}: {exc}")


def main() -> None:
    driver = SafariDriver()
    driver.start()
    try:
        safe_capture(driver, "01-home-ai-search.png", f"{BASE_URL}/")

        safe_capture(
            driver,
            "02-home-manual-search.png",
            f"{BASE_URL}/",
            """
            document.querySelector('#manualPeek .manual-open-pill')?.click();
            document.querySelector('input[name="origin"]').value = 'JFK';
            document.querySelector('input[name="destination"]').value = 'LAX';
            document.querySelector('#departPickerDisplay').value = 'Aug 5, 2026';
            document.querySelector('#returnPickerDisplay').value = 'Aug 12, 2026';
            """,
        )

        safe_capture(
            driver,
            "03-home-cheapest-week.png",
            f"{BASE_URL}/",
            """
            document.querySelector('#manualPeek .manual-open-pill')?.click();
            document.querySelector('[data-mode="flex"]')?.click();
            document.querySelector('input[name="origin"]').value = 'JFK';
            document.querySelector('input[name="destination"]').value = 'LAX';
            const flex = document.querySelector('#flexMonthPicker');
            if (flex) flex.value = 'Aug 2026';
            """,
        )

        driver.navigate(f"{BASE_URL}/")
        driver.js(
            """
            document.querySelector('#manualPeek .manual-open-pill')?.click();
            document.querySelector('#unifiedForm')?.setAttribute('action', '/search');
            document.querySelector('input[name="origin"]').value = 'JFK';
            document.querySelector('input[name="destination"]').value = 'LAX';
            document.querySelector('#departPicker').value = '2026-08-05';
            document.querySelector('#returnPicker').value = '2026-08-12';
            document.querySelector('#departPickerDisplay').value = 'Aug 5, 2026';
            document.querySelector('#returnPickerDisplay').value = 'Aug 12, 2026';
            document.querySelector('#unifiedForm')?.submit();
            """
        )
        found_results = driver.wait_for(
            """
            return document.body.innerText.includes('Select')
              || document.body.innerText.includes('No flights found')
              || document.body.innerText.includes('Nothing to show')
              || document.body.innerText.includes('Please include')
              || document.querySelector('a.fc-book, a[data-tier-cta], a[href*="/checkout/"]');
            """,
            timeout=75,
        )
        time.sleep(1.5)
        driver.screenshot("04-results-page.png")

        review_url = ""
        if found_results:
            review_url = driver.js(
                """
                const link = document.querySelector('a.fc-book, a[data-tier-cta], a[href*="/checkout/"]');
                return link ? link.href : '';
                """
            ) or ""

        if review_url:
            driver.navigate(review_url, delay=2.0)
            driver.screenshot("05-review-trip-collapsed.png")
            driver.js("document.querySelector('.review-flight-manifest summary')?.click();")
            time.sleep(0.9)
            driver.screenshot("06-review-trip-expanded.png")

            checkout_url = driver.js(
                """
                const link = document.querySelector('a.review-primary-cta, a[href*="/details"]');
                return link ? link.href : '';
                """
            ) or ""
            if checkout_url:
                driver.navigate(checkout_url, delay=2.0)
                driver.screenshot("07-checkout-summary-and-travelers.png")
                driver.js("window.scrollTo(0, document.body.scrollHeight * 0.55);")
                time.sleep(0.8)
                driver.screenshot("08-checkout-payment-section.png")
        else:
            print("No selectable live offer was returned, so review/checkout screenshots were skipped.")

        safe_capture(driver, "09-manage-booking.png", f"{BASE_URL}/manage-booking")
        safe_capture(driver, "10-auth-page.png", f"{BASE_URL}/auth")
        safe_capture(driver, "11-hotels-coming-soon.png", f"{BASE_URL}/hotels")
        safe_capture(driver, "12-deals-coming-soon.png", f"{BASE_URL}/deals")
    finally:
        driver.stop()


if __name__ == "__main__":
    main()
