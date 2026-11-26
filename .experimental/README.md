# experimental branch

This branch is upstream [discord.py](https://github.com/Rapptz/discord.py) master plus some unmerged pull
requests. It's rebuilt from scratch every day by `.github/workflows/experimental.yml`, and only updated when
discord.py's tests and the tests in `tests/` pass. `build.json` lists what the current build contains.

Install it in a bot:

```text
discord.py @ git+https://github.com/Ken-Miles/discord.py@experimental
```

## Files

| File | What it's for |
|---|---|
| `prs.json` | The pull requests to merge, each pinned to a commit, plus the fix patches |
| `build.py` | Rebuilds the branch. `python .experimental/build.py` builds and tests locally (the result is the `experimental-build` branch); `--push` also pushes it |
| `patches/` | Small fixes for problems in the pull requests themselves, plus `ci-drop-python-3.8.patch`, applied to every build (`"requires": null`) |
| `tests/` | One test per pull request, skipped when a build leaves that pull request out |
| `build.json` | Written by each build: the upstream commit, what was included and left out, and why |

## What updates by itself, and what doesn't

- **Upstream master** is followed automatically. The fork's `master` is kept an exact copy of it.
- **Pull requests stay at their pinned commit.** Anyone who can push to a pull request's branch could
  otherwise get code into this branch, and from there into the bots. When an author pushes new commits, the
  workflow opens an issue with a link to the changes. To take them, review the link and update that `sha` in
  `prs.json`.
- **Merged or closed pull requests** are left out of the build automatically, and the issue asks you to
  remove them from `prs.json`.
- **Conflicts** in `union_merge_files` (files where pull requests add things side by side, such as new
  enums) are resolved by keeping both sides. Any other conflict leaves that pull request out and opens the issue.
- **A fix patch that no longer applies** is left out and reported.

## Adding a pull request

Add `{"number": ..., "sha": "<its head commit>", "title": "..."}` to `prs.json`, add a test for it in
`tests/test_experimental_prs.py` with `@requires(<number>)`, and push. Pushing changes to `.experimental/`
starts a rebuild.

## One-time setup on GitHub

1. Enable Actions for the repository (Settings, Actions). GitHub disables them on forks.
2. Make `experimental` the default branch (Settings, General). Scheduled workflows only run on the default
   branch, and GitHub pauses them after 60 days without activity there.
