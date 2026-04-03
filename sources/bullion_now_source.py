from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from models import DealerMetalQuote, DealerSnapshot
from source_base import DealerSourceBase


class BullionNowSource(DealerSourceBase):
    def __init__(self) -> None:
        self._debug_dir = Path(__file__).resolve().parent.parent / "output" / "debug"
        self._debug_dir.mkdir(parents=True, exist_ok=True)

    @property
    def dealer_key(self) -> str:
        return "bullionNow"

    @property
    def source_url(self) -> str:
        return "https://bullionnow.com.au/"

    def fetch(self) -> DealerSnapshot:
        self._write_debug_file("bullionnow_fetch_started.txt", "fetch started")
        home_html = self._fetch_rendered_html(self.source_url)
        sell_text = self._normalize_html_to_text(home_html)

        self._write_debug_file("bullionnow_home_rendered.html", home_html)
        self._write_debug_file("bullionnow_home_rendered.txt", sell_text)

        gold_sell = self._extract_home_sell_price(sell_text, "Gold")
        if gold_sell is None:
            gold_sell = self._extract_product_price(
                "https://bullionnow.com.au/shop/perth-mint-gold-minted-bar-5g/"
            )

        silver_sell = self._extract_product_price(
            "https://bullionnow.com.au/shop/2026-perth-mint-silver-kangaroo-coin-1oz/"
        )
        if silver_sell is None:
            silver_sell = self._extract_home_sell_price(sell_text, "Silver")

        self._write_debug_file(
            "bullionnow_extracted_values.txt",
            f"gold_sell={gold_sell}\nsilver_sell={silver_sell}\n",
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
                sell=None,
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

    def _extract_home_sell_price(self, text: str, metal: str) -> float | None:
        if metal.lower() == "gold":
            gold_patterns = [
                re.compile(
                    r"Perth Mint Gold Cast Bar 1oz.*?Our price:\s*\$\s*([0-9,]+(?:\.[0-9]+)?)",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"1oz Gold.*?Our price:\s*\$\s*([0-9,]+(?:\.[0-9]+)?)",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"Gold Cast Bar 1oz.*?Our price:\s*\$\s*([0-9,]+(?:\.[0-9]+)?)",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"Perth Mint Gold Minted Bar 5g.*?Our price:\s*\$\s*([0-9,]+(?:\.[0-9]+)?)",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"Gold Minted Bar 5g.*?Our price:\s*\$\s*([0-9,]+(?:\.[0-9]+)?)",
                    re.IGNORECASE,
                ),
            ]

            for pattern in gold_patterns:
                match = pattern.search(text)
                if match:
                    value = self._parse_money(match.group(1))
                    if value is not None and value > 3000:
                        return value

            self._write_debug_file(
                "bullionnow_home_nomatch_gold.txt",
                text[:8000],
            )
            return None

        if metal.lower() == "silver":
            silver_patterns = [
                re.compile(
                    r"2026 Perth Mint Silver Kangaroo Coin 1oz.*?Our price:\s*\$\s*([0-9,]+(?:\.[0-9]+)?)",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"Silver Kangaroo Coin 1oz.*?Our price:\s*\$\s*([0-9,]+(?:\.[0-9]+)?)",
                    re.IGNORECASE,
                ),
                re.compile(
                    r"1oz Silver.*?Our price:\s*\$\s*([0-9,]+(?:\.[0-9]+)?)",
                    re.IGNORECASE,
                ),
            ]

            for pattern in silver_patterns:
                match = pattern.search(text)
                if match:
                    value = self._parse_money(match.group(1))
                    if value is not None and 10 < value < 500:
                        return value

            self._write_debug_file(
                "bullionnow_home_nomatch_silver.txt",
                text[:8000],
            )
            return None

        return None

    def _extract_product_price(self, url: str) -> float | None:
        safe_name = re.sub(r"[^a-z0-9]+", "_", url.lower()).strip("_")

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
                text = self._normalize_html_to_text(html)

                self._write_debug_file(f"{safe_name}.html", html)
                self._write_debug_file(f"{safe_name}.txt", text)

                meta_selectors = [
                    'meta[property="product:price:amount"]',
                    'meta[itemprop="price"]',
                    'meta[property="og:price:amount"]',
                ]

                for selector in meta_selectors:
                    try:
                        locator = page.locator(selector).first
                        if locator.count() > 0:
                            raw = locator.get_attribute("content")
                            value = self._parse_money(raw)
                            if value is not None:
                                self._write_debug_file(
                                    f"{safe_name}_extracted.txt",
                                    f"source=meta\nselector={selector}\nraw={raw}\nvalue={value}\n",
                                )
                                context.close()
                                browser.close()
                                return value
                    except Exception:
                        pass

                text_patterns = [
                    re.compile(r"Our price:\s*\$\s*([0-9,]+(?:\.[0-9]+)?)", re.IGNORECASE),
                    re.compile(r"Volume Discounts.*?1-9.*?\$\s*([0-9,]+(?:\.[0-9]+)?)", re.IGNORECASE),
                ]

                for pattern in text_patterns:
                    match = pattern.search(text)
                    if match:
                        value = self._parse_money(match.group(1))
                        if value is not None:
                            self._write_debug_file(
                                f"{safe_name}_extracted.txt",
                                f"source=text\npattern={pattern.pattern}\nraw={match.group(1)}\nvalue={value}\n",
                            )
                            context.close()
                            browser.close()
                            return value

                self._write_debug_file(
                    f"{safe_name}_extracted.txt",
                    "source=none\nvalue=None\n",
                )
                context.close()
                browser.close()
                return None

            except PlaywrightTimeoutError as e:
                context.close()
                browser.close()
                raise RuntimeError(f"Playwright timeout: {e}") from e
            except Exception:
                context.close()
                browser.close()
                raise

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