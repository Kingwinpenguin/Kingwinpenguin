import csv
import json
import unittest
from datetime import datetime, timedelta, timezone

from petersburg_fiber_rentals import (
    CSV_COLUMNS,
    Listing,
    cache_is_fresh,
    choose_prediction,
    classify_frontier,
    format_table,
    parse_listing_page,
    parse_price,
    parse_search_results,
    within_budget,
    write_csv,
)


SEARCH_HTML = """
<ol class="cl-static-search-results">
  <li class="cl-static-search-result" title="Main Street house">
    <a href="https://example.test/house">
      <div class="title">Main Street house</div>
      <div class="details"><div class="price">$950</div></div>
    </a>
  </li>
  <li class="cl-static-search-result" title="Over budget">
    <a href="https://example.test/high">
      <div class="title">Over budget</div>
      <div class="details"><div class="price">$1,400</div></div>
    </a>
  </li>
</ol>
"""

LISTING_HTML = """
<script type="application/ld+json">
{
  "@type": "House",
  "numberOfBedrooms": "2",
  "numberOfBathroomsTotal": "1",
  "address": {
    "streetAddress": "900 East Cherry St.",
    "addressLocality": "Petersburg",
    "addressRegion": "IN",
    "postalCode": "47567"
  }
}
</script>
<span class="price">$900</span>
"""


class ParseTests(unittest.TestCase):
    def test_price_and_budget(self) -> None:
        self.assertEqual(parse_price("$1,100"), 1100)
        self.assertTrue(within_budget(1100, 1100))
        self.assertFalse(within_budget(1101, 1100))
        self.assertFalse(within_budget(None, 1100))

    def test_search_cards(self) -> None:
        cards = parse_search_results(SEARCH_HTML)
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]["url"], "https://example.test/house")
        self.assertEqual(cards[0]["price"], "$950")

    def test_listing_page(self) -> None:
        listing = parse_listing_page(LISTING_HTML, "https://example.test/house")
        self.assertIsNotNone(listing)
        assert listing is not None
        self.assertEqual(listing.price, 900)
        self.assertIn("900 East Cherry", listing.address)
        self.assertIn("47567", listing.address)
        self.assertEqual(listing.beds_baths, "2 bd / 1 ba")


class FrontierTests(unittest.TestCase):
    def test_fiber_tier_beats_slower_tier(self) -> None:
        serviceability = {"success": True, "techAvailable": "FIBER"}
        products = {
            "availableProducts": [
                {"productId": "a", "name": "Fiber 500 Internet"},
                {"productCode": "fiber5gig", "name": "Fiber 5 Gig Internet"},
            ]
        }
        result = classify_frontier(serviceability, products)
        self.assertTrue(result.matched)
        self.assertEqual(result.max_speed, "Fiber 5 Gig")

    def test_copper_is_not_a_match_even_if_fiber_is_advertised(self) -> None:
        result = classify_frontier(
            {"success": True, "techAvailable": "COPPER"},
            {"availableProducts": [{"name": "Fiber 1 Gig Internet", "productId": "x"}]},
        )
        self.assertFalse(result.matched)
        self.assertEqual(result.max_speed, "")

    def test_dsl_product_name_is_not_fiber(self) -> None:
        result = classify_frontier(
            {"success": True, "techAvailable": "DSL"},
            {"availableProducts": [{"name": "High-Speed Internet 100", "productId": "d"}]},
        )
        self.assertFalse(result.matched)

    def test_future_fiber_flag_alone_is_not_a_match(self) -> None:
        result = classify_frontier({"success": False, "isFutureFiberEligible": True})
        self.assertFalse(result.matched)
        self.assertIn("not serviceable", result.reason.lower())

    def test_unserviceable_redirect(self) -> None:
        result = classify_frontier({"success": False, "redirect": {"url": "/unserviceable"}})
        self.assertFalse(result.matched)

    def test_marketing_copy_outside_a_product_is_ignored(self) -> None:
        result = classify_frontier(
            {
                "success": True,
                "techAvailable": "FIBER",
                "legal": {"disclaimer": "Ask about Fiber 7 Gig in select areas."},
            },
            {"legal": "Fiber 5 Gig"},
        )
        self.assertFalse(result.matched)

    def test_choose_exact_petersburg_address(self) -> None:
        candidates = [
            {
                "address": {"addressLine1": "900 E Cherry St", "city": "Jasper"},
                "parsedAddress": {"zipCodeBase": "47546"},
                "inFootprint": True,
                "isParent": False,
            },
            {
                "address": {"addressLine1": "900 East Cherry Street", "city": "Petersburg"},
                "parsedAddress": {"zipCodeBase": "47567"},
                "inFootprint": True,
                "isParent": True,
            },
            {
                "address": {"addressLine1": "900 East Cherry Street", "city": "Petersburg"},
                "parsedAddress": {"zipCodeBase": "47567"},
                "inFootprint": True,
                "isParent": False,
            },
        ]
        chosen = choose_prediction(candidates, "900 East Cherry St, Petersburg, IN 47567")
        self.assertIsNotNone(chosen)
        assert chosen is not None
        self.assertFalse(chosen["isParent"])
        self.assertEqual(chosen["address"]["city"], "Petersburg")

    def test_reject_different_house_number(self) -> None:
        candidates = [
            {
                "address": {"addressLine1": "902 East Cherry Street", "city": "Petersburg"},
                "parsedAddress": {"zipCodeBase": "47567"},
            }
        ]
        self.assertIsNone(choose_prediction(candidates, "900 East Cherry St, Petersburg, IN 47567"))


class OutputTests(unittest.TestCase):
    def test_csv_and_table(self) -> None:
        from pathlib import Path
        import tempfile

        row = {
            "Address": "900 East Cherry St, Petersburg, IN 47567",
            "Price": "$900",
            "Beds/Baths": "2 bd / 1 ba",
            "Max Confirmed Fiber Speed": "Fiber 1 Gig",
            "Listing URL": "https://example.test/house",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "fiber_rentals.csv"
            write_csv(path, [row])
            with path.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(tuple(reader.fieldnames or ()), CSV_COLUMNS)
                self.assertEqual(list(reader), [row])
        table = format_table([row])
        self.assertIn("Fiber 1 Gig", table)
        self.assertIn("900 East Cherry", table)

    def test_empty_table(self) -> None:
        self.assertIn("No listings", format_table([]))

    def test_cache_age(self) -> None:
        fresh = {"checked_at": datetime.now(timezone.utc).isoformat()}
        stale = {"checked_at": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()}
        self.assertTrue(cache_is_fresh(fresh))
        self.assertFalse(cache_is_fresh(stale))
        self.assertFalse(cache_is_fresh({}))

    def test_listing_row_shape(self) -> None:
        listing = Listing("1 Main St, Petersburg, IN 47567", 800, "1", "1", "https://example.test")
        self.assertEqual(listing.beds_baths, "1 bd / 1 ba")
        self.assertTrue(json.dumps(listing.address))


if __name__ == "__main__":
    unittest.main()
