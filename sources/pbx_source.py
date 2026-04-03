from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from models import DealerMetalQuote, DealerSnapshot
from source_base import DealerSourceBase


class PBXSource(DealerSourceBase):
    def __init__(self) -> None:
        self._debug_dir = Path(__file__).resolve().parent.parent / "output" / "debug"
        self._debug_dir.mkdir(parents=True, exist_ok=True)

    @property
    def dealer_key(self) -> str:
        return "pbx"

    @property
    def source_url(self) -> str:
        return "https://www.perthbullion.com.au/shop/"

    def fetch(self) -> DealerSnapshot:
        html = self._fetch_rendered_html(self.source_url)
        text = self._normalize_html_to_text(html)

        self._write_debug_file("pbx_rendered.html", html)
        self._write_debug_file("pbx_rendered.txt", text)

        gold_sell = self._extract_header_price(text, "GOLD")
        silver_sell = self._extract_header_price(text, "SILVER")
        platinum_sell = self._extract_header_price(text, "PLATINUM")

        self._write_debug_file(
            "pbx_extracted_values.txt",
            (
                f"gold_sell={gold_sell}\n"
                f"silver_sell={silver_sell}\n"
                f"platinum_sell={platinum_sell}\n"
                f"palladium_sell=None\n"
            ),
        )

        metals = {
            "Gold": DealerMetalQuote(
                buy=None,
                sell=gold_sell,
            ),
            "Silver": DealerMetalQuote(
                buy=None,
                sell=silver_sell,
            ),
            "Platinum": DealerMetalQuote(
                buy=None,
                sell=platinum_sell,
            ),
            "Palladium": DealerMetalQuote(
                buy=None,
                sell=None,
            ),
        }

        return self.build_success_snapshot(metals)

    def _fetch_rendered_html(self, url: str) -> str:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )

            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                locale="en-AU",
                timezone_id="Australia/Melbourne",
                viewport={"width": 1440, "height": 2200},
            )

            page = context.new_page()

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
                page.wait_for_timeout(6000)

                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(2000)
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_timeout(1000)
                except Exception:
                    pass

                html = page.content()
            except PlaywrightTimeoutError as e:
                context.close()
                browser.close()
                raise RuntimeError(f"Playwright timeout: {e}") from e
            except Exception:
                context.close()
                browser.close()
                raise

            context.close()
            browser.close()
            return html

    def _normalize_html_to_text(self, html: str) -> str:
        text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
        text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"<noscript[\s\S]*?</noscript>", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = (
            text.replace("&nbsp;", " ")
            .replace("&amp;", "&")
            .replace("&quot;", '"')
            .replace("&#36;", "$")
            .replace("&#x27;", "'")
            .replace("&#x2019;", "'")
            .replace("&#8211;", "-")
            .replace("&#8217;", "'")
        )
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def _extract_header_price(self, text: str, metal: str) -> float | None:
        patterns = [
            re.compile(
                rf"\b{re.escape(metal)}\s*:\s*\$([0-9,]+(?:\.[0-9]+)?)",
                re.IGNORECASE,
            ),
            re.compile(
                rf"\b{re.escape(metal)}\b\s*\$([0-9,]+(?:\.[0-9]+)?)",
                re.IGNORECASE,
            ),
        ]

        for pattern in patterns:
            match = pattern.search(text)
            if match:
                value = self._parse_money(match.group(1))
                if value is not None:
                    return value

        self._write_debug_file(
            f"pbx_nomatch_{metal.lower()}.txt",
            text[:8000],
        )
        return None

    def _parse_money(self, raw: str | None) -> float | None:
        if raw is None:
            return None

        cleaned = raw.replace(",", "").strip()
        try:
            value = float(cleaned)
        except Exception:
            return None

        if value <= 0:
            return None

        return round(value, 2)

    def _write_debug_file(self, name: str, content: str) -> None:
        path = self._debug_dir / name
        path.write_text(content, encoding="utf-8")