from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from models import DealerMetalQuote, DealerSnapshot
from source_base import DealerSourceBase


class PerthMintSource(DealerSourceBase):
    def __init__(self) -> None:
        self._debug_dir = Path(__file__).resolve().parent.parent / "output" / "debug"
        self._debug_dir.mkdir(parents=True, exist_ok=True)

    @property
    def dealer_key(self) -> str:
        return "perthMint"

    @property
    def source_url(self) -> str:
        return "https://www.perthmint.com/invest/information-for-investors/metal-prices/"

    def fetch(self) -> DealerSnapshot:
        html = self._fetch_rendered_html()
        text = self._normalize_html_to_text(html)

        self._write_debug_file("perthmint_rendered.html", html)
        self._write_debug_file("perthmint_rendered.txt", text)

        metals = self._extract_all_metals(text)

        return self.build_success_snapshot(metals)

    def _fetch_rendered_html(self) -> str:
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
                page.goto(
                    self.source_url,
                    wait_until="domcontentloaded",
                    timeout=90000,
                )

                page.wait_for_timeout(6000)

                try:
                    page.locator("text=Perth Mint buy and sell gold prices").first.wait_for(
                        timeout=12000
                    )
                except Exception:
                    pass

                page.wait_for_timeout(3000)

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

    def _extract_all_metals(self, text: str) -> dict[str, DealerMetalQuote]:
        return {
            "Gold": self._extract_metal(
                text,
                primary_markers=[
                    (
                        "Perth Mint buy and sell gold prices",
                        "Perth Mint buy and sell silver prices",
                    ),
                ],
                metal_name="Gold",
            ),
            "Silver": self._extract_metal(
                text,
                primary_markers=[
                    (
                        "Perth Mint buy and sell silver prices",
                        "Perth Mint buy and sell platinum prices",
                    ),
                ],
                metal_name="Silver",
            ),
            "Platinum": self._extract_metal(
                text,
                primary_markers=[
                    (
                        "Perth Mint buy and sell platinum prices",
                        "Perth Mint buy and sell palladium prices",
                    ),
                    (
                        "Perth Mint buy and sell platinum prices",
                        "Disclaimer",
                    ),
                ],
                metal_name="Platinum",
            ),
            "Palladium": self._extract_metal(
                text,
                primary_markers=[
                    (
                        "Perth Mint buy and sell palladium prices",
                        "Disclaimer",
                    ),
                ],
                metal_name="Palladium",
            ),
        }

    def _extract_metal(
        self,
        text: str,
        *,
        primary_markers: list[tuple[str, str]],
        metal_name: str,
    ) -> DealerMetalQuote:
        for start_marker, end_marker in primary_markers:
            section = self._slice_section(
                text,
                start_marker=start_marker,
                end_marker=end_marker,
            )

            if not section:
                continue

            quote = self._extract_best_quote_from_section(section)
            if quote.buy is not None or quote.sell is not None:
                self._write_debug_file(
                    f"perthmint_{metal_name.lower()}_matched.txt",
                    section[:4000],
                )
                return quote

            if "Metal pricing is unavailable at this time." in section:
                self._write_debug_file(
                    f"perthmint_{metal_name.lower()}_unavailable.txt",
                    section[:4000],
                )
                return DealerMetalQuote()

        return DealerMetalQuote()

    def _extract_best_quote_from_section(self, section: str) -> DealerMetalQuote:
        patterns = [
            re.compile(
                r"Australian Dollar\s+1 ounce\s+Perth Mint Sells\s+From\s+\$([0-9,]+(?:\.[0-9]+)?)\s+Perth Mint Buys\s+\$([0-9,]+(?:\.[0-9]+)?)",
                re.IGNORECASE,
            ),
            re.compile(
                r"Australian Dollar\s+1 ounce\s+Perth Mint Sells\s+\$([0-9,]+(?:\.[0-9]+)?)\s+Perth Mint Buys\s+\$([0-9,]+(?:\.[0-9]+)?)",
                re.IGNORECASE,
            ),
            re.compile(
                r"1 ounce\s+Perth Mint Sells\s+From\s+\$([0-9,]+(?:\.[0-9]+)?)\s+Perth Mint Buys\s+\$([0-9,]+(?:\.[0-9]+)?)",
                re.IGNORECASE,
            ),
            re.compile(
                r"1 ounce\s+Perth Mint Sells\s+\$([0-9,]+(?:\.[0-9]+)?)\s+Perth Mint Buys\s+\$([0-9,]+(?:\.[0-9]+)?)",
                re.IGNORECASE,
            ),
            re.compile(
                r"Australian Dollar\s+1 oz\s+Perth Mint Sells\s+From\s+\$([0-9,]+(?:\.[0-9]+)?)\s+Perth Mint Buys\s+\$([0-9,]+(?:\.[0-9]+)?)",
                re.IGNORECASE,
            ),
            re.compile(
                r"Australian Dollar\s+1 oz\s+Perth Mint Sells\s+\$([0-9,]+(?:\.[0-9]+)?)\s+Perth Mint Buys\s+\$([0-9,]+(?:\.[0-9]+)?)",
                re.IGNORECASE,
            ),
            re.compile(
                r"1 oz\s+Perth Mint Sells\s+From\s+\$([0-9,]+(?:\.[0-9]+)?)\s+Perth Mint Buys\s+\$([0-9,]+(?:\.[0-9]+)?)",
                re.IGNORECASE,
            ),
            re.compile(
                r"1 oz\s+Perth Mint Sells\s+\$([0-9,]+(?:\.[0-9]+)?)\s+Perth Mint Buys\s+\$([0-9,]+(?:\.[0-9]+)?)",
                re.IGNORECASE,
            ),
        ]

        for pattern in patterns:
            match = pattern.search(section)
            if match:
                sell = self._parse_money(match.group(1))
                buy = self._parse_money(match.group(2))
                return DealerMetalQuote(
                    buy=buy,
                    sell=sell,
                )

        return DealerMetalQuote()

    def _slice_section(
        self,
        text: str,
        *,
        start_marker: str,
        end_marker: str,
    ) -> str:
        start = text.find(start_marker)
        if start == -1:
            return ""

        end = text.find(end_marker, start + len(start_marker))
        if end == -1:
            return text[start:]

        return text[start:end]

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