from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

from models import DealerMetalQuote, DealerSnapshot
from source_base import DealerSourceBase


class ABCBullionSource(DealerSourceBase):
    def __init__(self) -> None:
        self._debug_dir = Path(__file__).resolve().parent.parent / "output" / "debug"
        self._debug_dir.mkdir(parents=True, exist_ok=True)

    @property
    def dealer_key(self) -> str:
        return "abcBullion"

    @property
    def source_url(self) -> str:
        return "https://www.abcbullion.com.au/pricing"

    @property
    def buyback_url(self) -> str:
        return "https://www.abcbullion.com.au/sell-metal"

    def fetch(self) -> DealerSnapshot:
        sell_html = self._fetch_rendered_html(self.source_url)
        buy_html = self._fetch_rendered_html(self.buyback_url)

        sell_text = self._normalize_html_to_text(sell_html)
        buy_text = self._normalize_html_to_text(buy_html)

        self._write_debug_file("abcbullion_sell_rendered.html", sell_html)
        self._write_debug_file("abcbullion_sell_rendered.txt", sell_text)
        self._write_debug_file("abcbullion_buy_rendered.html", buy_html)
        self._write_debug_file("abcbullion_buy_rendered.txt", buy_text)

        metals = {
            "Gold": DealerMetalQuote(
                buy=self._extract_buyback_price(buy_text, "Gold"),
                sell=self._extract_sell_price(sell_text, "Gold"),
            ),
            "Silver": DealerMetalQuote(
                buy=self._extract_buyback_price(buy_text, "Silver"),
                sell=self._extract_sell_price(sell_text, "Silver"),
            ),
            "Platinum": DealerMetalQuote(
                buy=self._extract_buyback_price(buy_text, "Platinum"),
                sell=self._extract_sell_price(sell_text, "Platinum"),
            ),
            "Palladium": DealerMetalQuote(
                buy=self._extract_buyback_price(buy_text, "Palladium"),
                sell=self._extract_sell_price(sell_text, "Palladium"),
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

    def _extract_sell_price(self, text: str, metal: str) -> float | None:
        patterns = [
            re.compile(
                rf"\bBUY\s+{re.escape(metal.upper())}\s+([0-9,]+(?:\.[0-9]+)?)\s*/oz\b",
                re.IGNORECASE,
            ),
            re.compile(
                rf"\bBUY\s+{re.escape(metal)}\s+([0-9,]+(?:\.[0-9]+)?)\s*/oz\b",
                re.IGNORECASE,
            ),
            re.compile(
                rf"\b{re.escape(metal)}\s+([0-9,]+(?:\.[0-9]+)?)/oz\b",
                re.IGNORECASE,
            ),
        ]

        for pattern in patterns:
            match = pattern.search(text)
            if match:
                return self._parse_money(match.group(1))

        return None

    def _extract_buyback_price(self, text: str, metal: str) -> float | None:
        patterns = [
            re.compile(
                rf"\b{re.escape(metal)}\s+Buyback\s+Price\s+\$?([0-9,]+(?:\.[0-9]+)?)\b",
                re.IGNORECASE,
            ),
            re.compile(
                rf"\b{re.escape(metal.upper())}\s+Buyback\s+Price\s+\$?([0-9,]+(?:\.[0-9]+)?)\b",
                re.IGNORECASE,
            ),
            re.compile(
                rf"\bBuyback\s+Price\s+{re.escape(metal)}\s+\$?([0-9,]+(?:\.[0-9]+)?)\b",
                re.IGNORECASE,
            ),
            re.compile(
                rf"\bSell\s+to\s+ABC\s+Bullion\s+{re.escape(metal)}\s+\$?([0-9,]+(?:\.[0-9]+)?)\b",
                re.IGNORECASE,
            ),
            re.compile(
                rf"\bLive\s+Buy\s*Back\s+Prices[\s\S]{{0,400}}?\b{re.escape(metal)}\b[\s\S]{{0,120}}?\$?([0-9,]+(?:\.[0-9]+)?)\b",
                re.IGNORECASE,
            ),
            re.compile(
                rf"\bView\s+Live\s+Buy\s*Back\s+Prices[\s\S]{{0,400}}?\b{re.escape(metal)}\b[\s\S]{{0,120}}?\$?([0-9,]+(?:\.[0-9]+)?)\b",
                re.IGNORECASE,
            ),
            re.compile(
                rf"\bBUY\s+{re.escape(metal.upper())}\s+([0-9,]+(?:\.[0-9]+)?)/oz\b",
                re.IGNORECASE,
            ),
            re.compile(
                rf"\bBUY\s+{re.escape(metal)}\s+([0-9,]+(?:\.[0-9]+)?)/oz\b",
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
            f"abcbullion_buy_nomatch_{metal.lower()}.txt",
            text[:6000],
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