"""Offline browser contracts. Optional: /usr/bin/python3 this_file.py.

No browser dependency is added to the default test environment. Tests exercise
file:// imports/downloads and real DOM behavior, not JavaScript internals.
"""
import json
import os
import shutil
import unittest
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

HTML = Path(__file__).resolve().parents[1] / "execution" / "visualizer.html"
CHROME = os.environ.get("WAVEFLOW_TEST_CHROME") or shutil.which("google-chrome")


@unittest.skipUnless(sync_playwright and CHROME, "optional Playwright/Chrome unavailable")
class VisualizerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.driver = sync_playwright().start()
        cls.browser = cls.driver.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.driver.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport={"width": 1200, "height": 900})
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))

    def tearDown(self):
        self.assertEqual(self.errors, [])
        self.page.close()

    def import_bundle(self, bundle):
        self.page.locator("#import-file").set_input_files({
            "name": "study.json", "mimeType": "application/json",
            "buffer": json.dumps(bundle).encode(),
        })
        self.page.wait_for_function("document.querySelector('#message').textContent !== 'Loading…'")

    @staticmethod
    def transport_fixture():
        # Deliberately unavailable test-only observations, never shipped evidence.
        experiments = []
        for index, design in enumerate(("fir", "mem_copy")):
            experiment = f"test-experiment-{design}"
            experiments.append({
                "design_id": design, "experiment_id": experiment,
                "description": {"design_id": design, "parameter_schema": {"width": {"type": "integer"}},
                                "conditions": {"fixed_test": "test-only"},
                                "metric_definitions": {}, "capabilities": {"synth": False}},
                "runs": [{"schema_version": "waveflow-execution-v1", "run_id": f"test-run-{design}",
                          "request_id": f"test-request-{design}", "experiment_id": experiment,
                          "design_id": design, "operation": "synth", "params": {"width": 8},
                          "status": "unavailable", "metrics": {}, "checks": [],
                          "timing": {"wall_seconds": index + 1}, "artifacts": [],
                          "evidence_kind": "test-only-unavailable",
                          "error": "Test-only transport fixture: no hardware execution"}],
            })
        return {"schema_version": "waveflow-study-v1", "objective": "Test-only boundary review",
                "generated_at": "2026-01-01T00:00:00Z", "source_revision": "test-only-revision",
                "experiments": experiments,
                "review_criteria": [{"id": "hardware", "label": "Hardware continuity",
                                     "status": "not_checked", "evidence": "No hardware was run"}]}

    def test_import_filter_detail_and_review_download_roundtrip(self):
        self.page.goto(HTML.as_uri())
        bundle = self.transport_fixture()
        self.import_bundle(bundle)
        self.page.wait_for_selector(".run-row")
        self.assertEqual(self.page.locator(".run-row").count(), 2)
        self.assertEqual(self.page.locator("#objective").inner_text(), bundle["objective"])
        self.page.locator("#design-filter").select_option("mem_copy")
        self.assertEqual(self.page.locator(".run-row").count(), 1)
        self.assertIn("fixed_test", self.page.locator("#experiment-list").inner_text())
        self.page.locator(".run-row").click()
        self.assertIn("test-request-mem_copy", self.page.locator("#run-detail").inner_text())
        self.assertIn("test-only-unavailable", self.page.locator("#run-detail").inner_text())
        self.assertIn("2", self.page.locator("#timing-chart").inner_text())
        self.assertIn("not_checked", self.page.locator("#criteria").inner_text())
        self.page.locator("#operation-filter").select_option("pysim")
        self.assertEqual(self.page.locator(".run-row").count(), 0)
        self.assertIn("No attempts match", self.page.locator("#run-count").inner_text())
        self.assertIn("No attempt selected", self.page.locator("#run-detail").inner_text())
        self.page.locator("#operation-filter").select_option("")
        self.page.locator("#review-notes").fill("Designed: boundary intact. Worked: hardware unverified.")
        self.page.locator("#proposal").fill("Try a matched workload on a provisioned toolchain.")
        with self.page.expect_download() as download:
            self.page.locator("#download-review").click()
        saved = json.loads(Path(download.value.path()).read_text())
        self.assertEqual(saved["experiments"], bundle["experiments"])
        self.assertEqual(saved["review_criteria"], bundle["review_criteria"])
        self.assertIn("hardware unverified", saved["review"]["notes"])
        self.page.reload()
        self.import_bundle(saved)
        self.page.wait_for_selector(".run-row")
        self.assertIn("hardware unverified", self.page.locator("#review-notes").input_value())
        self.assertIn("matched workload", self.page.locator("#proposal").input_value())
        self.assertEqual(self.page.locator("#design-filter").input_value(), "mem_copy")
        self.assertEqual(self.page.locator(".run-row").count(), 1)

    def test_rejects_malformed_import_without_replacing_loaded_review(self):
        from copy import deepcopy
        self.page.goto(HTML.as_uri())
        baseline = self.transport_fixture()
        self.import_bundle(baseline)
        mutations = [
            lambda b: b.update(schema_version="unrelated"),
            lambda b: b.update(experiments={}),
            lambda b: b["experiments"][0]["runs"][0]["timing"].update(wall_seconds=-1),
            lambda b: b["experiments"][0]["runs"][0]["timing"].update(wall_seconds=1e309),
            lambda b: b["experiments"][0]["runs"][0].update(design_id="wrong"),
            lambda b: b["experiments"][0]["runs"][0].update(experiment_id="wrong"),
            lambda b: b["experiments"][0]["runs"].append(deepcopy(b["experiments"][0]["runs"][0])),
            lambda b: b["experiments"].append(deepcopy(b["experiments"][0])),
            lambda b: b["experiments"][0]["runs"][0].update(metrics={"bad": 1}),
            lambda b: b.update(review={"notes": [], "proposal": ""}),
        ]
        for mutate in mutations:
            candidate = deepcopy(baseline)
            mutate(candidate)
            self.import_bundle(candidate)
            self.assertIn("Import rejected", self.page.locator("#message").inner_text())
            self.assertEqual(self.page.locator(".run-row").count(), 2)
            self.assertEqual(self.page.locator("#objective").inner_text(), baseline["objective"])

    def test_safe_text_and_responsive_connected_boundary(self):
        self.page.goto(HTML.as_uri())
        bundle = self.transport_fixture()
        attack = '<img src=x onerror="window.injected=true">'
        bundle["objective"] = attack
        run = bundle["experiments"][0]["runs"][0]
        run["artifacts"] = {"evidence": attack}
        run["metrics"] = {"test_metric": {"value": 3, "unit": "test-unit", "kind": "test-only", "definition": attack}}
        self.import_bundle(bundle)
        self.assertEqual(self.page.locator("#objective").inner_text(), attack)
        self.assertIn("3 test-unit", self.page.locator("#run-detail").inner_text())
        self.assertEqual(self.page.locator("img").count(), 0)
        self.assertIsNone(self.page.evaluate("window.injected"))
        self.assertEqual(self.page.locator(".bar").evaluate_all("els=>els.map(e=>e.style.width)"), ["50%", "100%"])
        self.assertIn("Solid", self.page.locator("#architecture").inner_text())
        self.assertIn("Dashed", self.page.locator("#architecture").inner_text())
        bundle["objective"] = "Test-only boundary review · unavailable observations, not hardware validation"
        run["metrics"]["test_metric"]["definition"] = "Transport fixture only; not a domain measurement."
        self.import_bundle(bundle)
        for width in (1200, 700, 390):
            self.page.set_viewport_size({"width": width, "height": 900})
            self.assertTrue(self.page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            self.assertEqual(self.page.locator("p,button,label,select,dt,dd").evaluate_all(
                "els=>els.filter(e=>parseFloat(getComputedStyle(e).fontSize)<16).length"), 0)
            self.page.get_by_role("button", name="rtlsim", exact=True).click()
            self.assertIn("exact successful synthesis artifact", self.page.locator("#stage-detail").inner_text())
            screenshot_dir = os.environ.get("WAVEFLOW_SCREENSHOTS")
            if screenshot_dir:
                Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
                self.page.screenshot(path=str(Path(screenshot_dir) / f"visualizer-{width}.png"), full_page=True)
                self.page.locator("#architecture .panel").screenshot(path=str(Path(screenshot_dir) / f"diagram-{width}.png"))

    def test_json_schema_domains_are_readable_not_one_properties_blob(self):
        self.page.goto(HTML.as_uri())
        bundle = self.transport_fixture()
        bundle["experiments"][0]["description"]["parameter_schema"] = {
            "type": "object", "properties": {
                "mem_dwidth": {"type": "integer", "enum": [32, 64], "default": 64},
                "lanes": {"type": "integer", "minimum": 1, "maximum": 8}}}
        self.import_bundle(bundle)
        names = self.page.locator("#experiment-list dt").all_text_contents()
        self.assertIn("mem_dwidth", names)
        self.assertIn("lanes", names)
        self.assertNotIn("properties", names)
        self.assertIn("Choices: 32, 64", self.page.locator("#experiment-list").inner_text())

    def test_discussion_sends_notes_and_evidence_references_not_source_dump(self):
        self.page.goto(HTML.as_uri())
        self.import_bundle(self.transport_fixture())
        self.assertEqual(self.page.locator("#discuss-review").count(), 1)
        self.page.locator("#review-notes").fill("Explain the artifact boundary")
        self.page.locator("#discuss-review").click()
        self.assertIn("Download", self.page.locator("#discussion-status").inner_text())
        self.page.evaluate("window.hermes={send:message=>{window.sentReview=message}}")
        self.page.locator("#discuss-review").click()
        sent = self.page.evaluate("window.sentReview")
        self.assertIn("Explain the artifact boundary", sent)
        self.assertIn("test-run-fir", sent)
        self.assertNotIn('"sources":', sent)

    def test_empty_template_keeps_objective_and_exposes_no_claimed_runs(self):
        self.assertTrue(HTML.exists(), "execution review template is not implemented")
        self.page.goto(HTML.as_uri())
        self.assertIn("parameter", self.page.locator("#objective").inner_text())
        self.assertIn("No loaded evidence", self.page.locator("#run-count").inner_text())
        self.assertEqual(self.page.locator(".run-row").count(), 0)
        self.page.get_by_role("button", name="synth", exact=True).click()
        self.assertIn("artifact", self.page.locator("#stage-detail").inner_text())


if __name__ == "__main__":
    unittest.main()
