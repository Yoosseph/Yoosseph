# Neural GitHub statistics

`generate-stats.py` uses only the Python standard library. The template includes
inline logo paths, icons, gradients, styles and lightweight CSS animations. The
generated SVG requires no external assets. It renders a static card if animations
are unsupported and respects reduced motion. Each statistic lives inside a
network node. Below 600px, the same nodes reflow into two columns with larger
labels and values while retaining the central GitHub node and its connections.
Each node sends two signals into GitHub every 2.8 seconds, with varied offsets
so signals overlap naturally. The central ring keeps a subtle heartbeat rhythm.

## Automatic updates

`.github/workflows/update-stats.yml` runs every six hours at
**04:23, 10:23, 16:23 and 22:23 UTC**, manually from
Actions → Update neural GitHub stats → Run workflow, and on generator/template/
workflow changes on `main`. It tests the generator, fetches all statistics,
validates the SVG as XML, then commits only `assets/neural-github-stats.svg` if it
changed. Bot commits do not trigger another update. Pushes use the built-in
`GITHUB_TOKEN` with `contents: write`. Branch protections must allow the bot to push.

No additional secret is required for public profile statistics. Optionally create
an Actions repository secret named `GH_STATS_TOKEN` to include private activity
visible to an owner token (a classic PAT uses `read:user` and `repo` for private
contributions). This token is used only for API reads; the built-in token still
commits the SVG. Only aggregate numbers are published, never tokens or repository
identifiers. The initial local generation uses the existing authenticated CLI.

GitHub can disable scheduled workflows in inactive public repositories after 60
days; re-enable in Actions if necessary. API failures or incomplete commit search
results fail the job and leave the last successful card untouched.

## Local use

```sh
GITHUB_TOKEN=... python scripts/generate-stats.py --username Yoosseph
# Or reuse an existing CLI login without printing/extracting its token:
python scripts/generate-stats.py --username Yoosseph --use-gh
python -m unittest discover -s scripts -p 'test_*.py'
```

## Counting rules

- **Stars:** sum across all owned repositories, including forks, fully paginated
  (same ownership scope as the old card).
- **Contributions:** sum of contribution-calendar totals across every available
  `contributionYears` entry, including the current year. Imported commits can
  predate signup. Calendar totals follow GitHub's eligibility rules and include
  review and repository-creation activity.
- **Commits:** all-time REST commit search `author:USERNAME`, matching the old
  card's `include_all_commits=true` behavior. Indexed commits and contribution
  calendar commits are different metrics, so commits need not equal contributions.
- **Issues / PRs:** all-time authored issue and pull-request connection totals.
- **Contributed to (all time):** unique repository IDs across commit, issue,
  pull-request and repository-creation contributions in every available year.
  Includes own repositories where activity exists. Connections are paginated;
  intervals exceeding the 100 returned commit repositories are split and unioned.
  Avoids `repositoriesContributedTo`, which GitHub documents as *recent* activity.

Counts reflect what GitHub exposes to the API token. The built-in Actions token
generally sees public activity and this repository; an owner token may see
additional private activity. Unavailable/deleted/restricted repository identities
cannot be recovered for the unique count. Calendar totals can include anonymous
private contribution counts when enabled.

Large values use compact M/B/T notation, with exact values in accessibility text
and individual SVG value titles. SVG size is independent of repository count.

References: [GraphQL User/ContributionsCollection](https://docs.github.com/en/graphql/reference/users),
[REST commit search](https://docs.github.com/en/rest/search/search#search-commits),
[workflow tokens](https://docs.github.com/en/actions/tutorials/authenticate-with-github_token),
[inactive schedules](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows).

## GitHub mark license

Inline logo: Primer's
[`mark-github-16` Octicon](https://github.com/primer/octicons/blob/main/icons/mark-github-16.svg).
Copyright (c) 2026 GitHub Inc. Used under the [MIT license](https://github.com/primer/octicons/blob/main/LICENSE).

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
the Software, and to permit persons to whom the Software is furnished to do so,
subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
