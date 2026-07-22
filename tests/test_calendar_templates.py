from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
INDEX_TEMPLATE = ROOT / "templates" / "index.html"
RESULTS_TEMPLATE = ROOT / "templates" / "results.html"
CALENDAR_JS = ROOT / "static" / "calendar.js"


class CalendarTemplateTests(unittest.TestCase):
    def test_index_uses_shared_calendar_assets_and_month_picker(self):
        html = INDEX_TEMPLATE.read_text()

        self.assertIn('id="departPickerDisplay"', html)
        self.assertIn('id="returnPickerDisplay"', html)
        self.assertIn('id="departPicker"', html)
        self.assertIn('id="returnPicker"', html)
        self.assertIn('id="flexMonthPicker"', html)
        self.assertIn("flatpickr.min.css", html)
        self.assertIn("monthSelect/style.css", html)
        self.assertIn("static', filename='calendar.js')", html)
        self.assertIn("static', filename='search-loader.js')", html)
        self.assertIn('id="loaderStage"', html)
        self.assertNotIn('type="month"', html)

    def test_results_uses_shared_calendar_assets_and_no_native_date_inputs(self):
        html = RESULTS_TEMPLATE.read_text()

        self.assertIn('id="resultsQueryForm"', html)
        self.assertIn('id="resultsTripType"', html)
        self.assertIn('id="resultsSortInput"', html)
        self.assertIn("requested_trip_type", html)
        self.assertIn('id="refineDepartPickerDisplay"', html)
        self.assertIn('id="refineReturnPickerDisplay"', html)
        self.assertIn('id="refineDepartPicker"', html)
        self.assertIn('id="refineReturnPicker"', html)
        self.assertIn("flatpickr.min.css", html)
        self.assertIn("static', filename='calendar.js')", html)
        self.assertIn("static', filename='search-loader.js')", html)
        self.assertIn('id="loaderStage"', html)
        self.assertNotIn("seg.time_note", html)
        self.assertNotIn('type="date"', html)
        self.assertNotIn("({{ f.airline_code_summary }})", html)
        self.assertNotIn("{% if seg.airline_code %}", html)
        self.assertIn("url_for('checkout_offer', offer_id=f.offer_id)", html)
        self.assertNotIn("f.booking_url", html)

    def test_shared_calendar_script_handles_range_dates_and_months(self):
        js = CALENDAR_JS.read_text()

        self.assertIn("initSharedDateRange", js)
        self.assertIn('mode", oneWay ? "single" : "range"', js)
        self.assertIn("syncDatePairValues", js)
        self.assertIn("monthSelectPlugin", js)
        self.assertIn("flexMonthPicker", js)
        self.assertIn("nx-calendar-month", js)
        self.assertIn("clampCalendarToViewport", js)


if __name__ == "__main__":
    unittest.main()
