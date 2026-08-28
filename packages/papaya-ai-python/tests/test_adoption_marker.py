import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from papaya_ai import APPLIED_RECOMMENDATION_WINDOW, Papaya
from papaya_ai.core import _control_bag

# Deliberately shared with the TypeScript SDK, which reads this same file from
# its own suite. The Supernova monorepo and standalone public SDK repository use
# different package roots, so resolve both layouts while keeping one fixture in
# either checkout. A silent divergence would drop customer adoption markers on
# one runtime only, which is exactly the bug this guards against.
REPO_ROOT = ROOT.parent.parent
PARITY_FIXTURE_CANDIDATES = (
    REPO_ROOT / "packages" / "papaya-ai" / "test" / "adoption-marker-parity.json",
    REPO_ROOT / "test" / "adoption-marker-parity.json",
)
PARITY_FIXTURE = next((candidate for candidate in PARITY_FIXTURE_CANDIDATES if candidate.is_file()), None)
if PARITY_FIXTURE is None:
    raise FileNotFoundError(f"Missing shared adoption-marker parity fixture in {REPO_ROOT}")


class AdoptionMarkerTest(unittest.TestCase):
    def setUp(self):
        self.fixture = json.loads(PARITY_FIXTURE.read_text())

    def test_matches_the_shared_cross_sdk_parity_fixture(self):
        for case in self.fixture["cases"]:
            with self.subTest(case=case["name"]):
                self.assertEqual(_control_bag(case["input"]), case["expected"])

    def test_window_matches_the_fixture_and_the_typescript_sdk(self):
        self.assertEqual(APPLIED_RECOMMENDATION_WINDOW, self.fixture["window"])

    def test_window_keeps_the_newest_markers(self):
        many = [f"agfind-{1756089600000 + index}-abcdefab" for index in range(APPLIED_RECOMMENDATION_WINDOW + 5)]
        bag = _control_bag(many)
        assert bag is not None
        markers = bag["appliedRecommendations"]
        self.assertEqual(len(markers), APPLIED_RECOMMENDATION_WINDOW)
        # The tail, not the head: a customer config grows for the life of the
        # application, but the payload must not.
        self.assertEqual(markers, many[-APPLIED_RECOMMENDATION_WINDOW:])
        self.assertNotIn(many[0], markers)

    def test_does_not_mutate_the_callers_list(self):
        original = ["agfind-1-aaaa", "agfind-2-bbbb", "agfind-1-aaaa"]
        snapshot = list(original)
        _control_bag(original)
        self.assertEqual(original, snapshot)

    def test_omits_the_bag_entirely_when_there_is_nothing_to_send(self):
        self.assertIsNone(_control_bag(None))
        self.assertIsNone(_control_bag([]))
        self.assertIsNone(_control_bag(["junk"]))
        self.assertIsNone(_control_bag("not-a-list"))

    def test_bag_rides_the_batch_envelope_and_never_the_trace_metadata(self):
        marker = "agfind-1756089600000-aaaaaaaa"
        captured = []

        def transport(endpoint, headers, body):
            captured.append(json.loads(body.decode("utf-8")))
            return 202, '{"accepted":1,"rejected":0}'

        papaya = Papaya(
            api_key="papaya-test-token",
            endpoint="https://papaya.example/api/v1/ingest/traces",
            transport=transport,
            metadata={"plan": "standard"},
            applied_recommendations=[marker],
        )
        with papaya.run({"workflowKey": "checkout"}):
            pass
        papaya.flush()

        self.assertEqual(len(captured), 1)
        batch = captured[0]
        self.assertEqual(batch["papaya"], {"appliedRecommendations": [marker]})
        # The customer's own run metadata is exported untouched; the bag is an
        # addition, not a replacement.
        self.assertEqual(batch["traces"][0]["metadata"], {"plan": "standard"})
        # Markers must never reach trace content, which is customer data and is
        # redacted server-side by capture policy.
        self.assertNotIn(marker, json.dumps(batch["traces"]))

    def test_an_unconfigured_client_omits_the_key_entirely(self):
        captured = []

        def transport(endpoint, headers, body):
            captured.append(json.loads(body.decode("utf-8")))
            return 202, '{"accepted":1,"rejected":0}'

        papaya = Papaya(
            api_key="papaya-test-token",
            endpoint="https://papaya.example/api/v1/ingest/traces",
            transport=transport,
        )
        with papaya.run({"workflowKey": "checkout"}):
            pass
        papaya.flush()

        # Indistinguishable from a batch built before this feature existed:
        # the key absent, never present-and-null.
        self.assertNotIn("papaya", captured[0])


if __name__ == "__main__":
    unittest.main()
