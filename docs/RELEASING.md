# Releasing Noshy to PyPI

Noshy uses **PyPI's trusted publishing (OIDC)** — no API token needs to live
anywhere. The release workflow in `.github/workflows/release.yml` does
everything; you just push a tag.

## One-time setup

1. **Reserve the name on PyPI.** Go to https://pypi.org/manage/account/publishing/
   and add a new "pending" publisher:
   - PyPI project name: `noshy`
   - Owner: `noshkoto`
   - Repository: `Noshy`
   - Workflow filename: `release.yml`
   - Environment name: `pypi`
2. **(Optional) Repeat for TestPyPI** at https://test.pypi.org/manage/account/publishing/
   with environment name `testpypi`. Useful for dry runs.
3. **Create the GitHub environments** in repo Settings → Environments:
   - `pypi` — add a required reviewer if you want a manual gate
   - `testpypi` — no protection needed

After the first successful publish, the "pending" publisher converts into a
real one and stays linked to the project.

## Cutting a release

```bash
# 1. Bump the version in pyproject.toml
$EDITOR pyproject.toml
# 2. Commit + tag
git commit -am "release: v0.3.0"
git tag v0.3.0
git push && git push --tags
```

The tag push triggers `release.yml`, which:

1. Verifies the tag matches `project.version` in `pyproject.toml`
2. Builds the sdist + wheel
3. Runs `twine check` for metadata sanity
4. Uploads to PyPI via OIDC
5. Creates a GitHub release with auto-generated notes and attaches the wheel

## Dry-running on TestPyPI

From the Actions tab, "Run workflow" on Release → choose **testpypi**. Then:

```bash
pip install -i https://test.pypi.org/simple/ noshy
```
