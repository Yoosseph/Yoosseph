"""Exercise totals, API limits, failure preservation and SVG portability offline."""

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location("stats", Path(__file__).with_name("generate-stats.py"))
stats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stats)


def page(nodes=(), next_cursor=None):
    return {"nodes": list(nodes), "pageInfo": {
        "hasNextPage": next_cursor is not None, "endCursor": next_cursor,
    }}


def collection(total=10, repos=("shared",), issue_page=None):
    return {"contributionsCollection": {
        "contributionCalendar": {"totalContributions": total},
        "totalRepositoriesWithContributedCommits": len(repos),
        "commitContributionsByRepository": [{"repository": {"id": r}} for r in repos],
        "issueContributions": issue_page or page(),
        "pullRequestContributions": page([{"pullRequest": {"repository": {"id": "shared"}}}]),
        "repositoryContributions": page(),
    }}


class StatisticsTests(unittest.TestCase):
    def test_all_years_pagination_and_unique_repositories(self):
        api = Mock()
        requested_years = []

        def user(query, variables):
            if query == stats.PROFILE_QUERY:
                return {"login": "Yoosseph", "issues": {"totalCount": 7},
                        "pullRequests": {"totalCount": 11},
                        "contributionsCollection": {"contributionYears": [2025, 2015]}}
            if query == stats.STARS_QUERY:
                return {"repositories": page([{"stargazerCount": 3}], "next")
                        if variables["after"] is None else page([{"stargazerCount": 5}])}
            if query == stats.COLLECTION_QUERY:
                requested_years.append(variables["from"][:4])
                return collection(issue_page=page([{"issue": {"repository": {"id": "shared"}}}], "more"))
            self.assertIn("issueContributions", query)
            self.assertIn("issue { repository { id } }", query)
            return {"contributionsCollection": {"issueContributions": page([
                {"issue": {"repository": {"id": "historic"}}}, {"issue": None},
            ])}}

        api.user.side_effect = user
        api.request.return_value = {"total_count": 42, "incomplete_results": False}
        login, result, updated = stats.fetch_stats(api, "Yoosseph", datetime(2026, 10, 3, tzinfo=timezone.utc))
        self.assertEqual(requested_years, ["2015", "2025", "2026"])
        self.assertEqual(result, {"stars": 8, "contributions": 30, "commits": 42,
                                  "issues": 7, "prs": 11, "repositories": 2})
        self.assertEqual((login, updated), ("Yoosseph", "2026-10-03"))

    def test_commit_repository_limit_splits_without_double_counting_calendar(self):
        api = Mock()
        truncated = collection(total=100, repos=("a",))
        truncated["contributionsCollection"]["totalRepositoriesWithContributedCommits"] = 2
        api.user.side_effect = [truncated, collection(total=60, repos=("a",)), collection(total=60, repos=("b",))]
        start, end = datetime(2025, 1, 1, tzinfo=timezone.utc), datetime(2025, 12, 31, tzinfo=timezone.utc)
        count, repositories = stats.collect_interval(api, "Yoosseph", start, end)
        self.assertEqual(count, 100)  # Not the overlapping split calendar sum (120).
        self.assertEqual(repositories, {"a", "b", "shared"})
        self.assertEqual(api.user.call_count, 3)

    def test_repeated_pagination_cursor_fails(self):
        with self.assertRaises(stats.StatsError):
            list(stats.pages(page([], "same"), lambda _: page([], "same")))

    def test_incomplete_search_never_publishes_totals(self):
        api = Mock()
        api.user.side_effect = [
            {"login": "Yoosseph", "issues": {"totalCount": 1}, "pullRequests": {"totalCount": 1}},
            {"repositories": page()},
        ]
        api.request.return_value = {"total_count": 0, "incomplete_results": True}
        with patch.object(stats.time, "sleep"), self.assertRaises(stats.StatsError):
            stats.fetch_stats(api, "Yoosseph")
        self.assertEqual(api.request.call_count, 3)

    def test_graphql_partial_errors_are_rejected(self):
        with patch.dict(stats.os.environ, {"GITHUB_TOKEN": "test-token"}):
            api = stats.GitHubAPI()
        api.request = Mock(return_value={"errors": [{"message": "denied"}], "data": {"user": {"login": "x"}}})
        with self.assertRaises(stats.StatsError):
            api.user(stats.PROFILE_QUERY, {"login": "x"})


class SVGTests(unittest.TestCase):
    def test_large_numbers_xml_escaping_and_no_missing_assets(self):
        values = dict.fromkeys([key for key, _, _ in stats.METRICS], 123456789012345)
        svg = stats.render_svg('name<&"', values, "2026-10-03")
        root = ET.fromstring(svg)
        self.assertEqual(root.attrib["viewBox"], "0 0 880 548")
        self.assertIn("123.5T", svg)
        self.assertIn("123,456,789,012,345", svg)
        self.assertNotIn("{{", svg)
        self.assertLess(len(svg.encode()), 25000)
        ids = {element.get("id") for element in root.iter() if element.get("id")}
        references = set(re.findall(r"url\(#([\w-]+)\)", svg))
        self.assertTrue(references <= ids)
        for element in root.iter():
            self.assertNotIn(element.tag.rsplit("}", 1)[-1], ("script", "image", "foreignObject"))
            self.assertFalse(any("href" in key and not value.startswith("#") for key, value in element.attrib.items()))

    def test_invalid_xml_preserves_existing_card_and_identical_render_skips_write(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "card.svg"
            valid = stats.render_svg("Yoosseph", {key: 0 for key, _, _ in stats.METRICS}, "2026-10-03")
            self.assertTrue(stats.write_svg(output, valid))
            self.assertFalse(stats.write_svg(output, valid))
            with self.assertRaises(ET.ParseError):
                stats.write_svg(output, "<svg>broken")
            self.assertEqual(output.read_text(encoding="utf-8"), valid)

    def test_negative_and_non_integer_stats_rejected(self):
        values = {key: 0 for key, _, _ in stats.METRICS}
        for invalid in (-1, True, "12"):
            with self.assertRaises(stats.StatsError):
                stats.render_svg("Yoosseph", {**values, "stars": invalid}, "2026-10-03")


if __name__ == "__main__":
    unittest.main()
