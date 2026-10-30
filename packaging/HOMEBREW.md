# Publishing delta-wifi with Homebrew

This project uses a **personal tap**, not Homebrew's central package repository. A tap is a normal GitHub repository holding a Ruby installation recipe. There is no submission or review process with the Homebrew maintainers for this route.

Two public MIT repositories:

- `chiesennegs/delta-wifi`: source, tests, version tags, release archive, checksum, and generated formula.
- `chiesennegs/homebrew-tap`: `Formula/delta-wifi.rb` and a workflow to update it from a published release.

## What users will run

After the first release is published and the tap recipe is present:

```sh
brew install chiesennegs/tap/delta-wifi
delta-wifi wifi-perms
delta-wifi login
```

Homebrew manages the Python runtime and its isolated environment. Native helpers are precompiled for Apple Silicon and Intel. Users do not activate a venv or compile Swift on first use.

Upgrades:

```sh
brew update
brew upgrade delta-wifi
delta-wifi --version
```

Keychain credentials, preferences, and history are outside the Homebrew installation. Prebuilt helpers are copied to a stable private location under Application Support. Ordinary upgrades/uninstalls do not delete user state.

The formula ships the HTTP-only core. For optional browser mode, an alternative no-manual-venv development invocation is:

```sh
uvx --from 'delta-wifi[browser] @ git+https://github.com/chiesennegs/delta-wifi.git@v0.3.0' delta-wifi login --mode browser --browser canary
```

That source-based optional route may compile helpers on first permission setup; Homebrew's normal HTTP installation uses precompiled helpers.

## First-time repository setup

From the project directory, once the local release files have been reviewed:

```sh
git init -b main
git add .
git commit -m 'Prepare delta-wifi for distribution'
gh repo create chiesennegs/delta-wifi --public --source . --remote origin --push
```

The `.gitignore` excludes the development venv, compiled local apps, credentials-file patterns, and build output. The release builder uses an explicit source allowlist, so local state and research files cannot enter the release archive.

Create the separate tap from the prepared tap directory:

```sh
gh repo create chiesennegs/homebrew-tap --public --source PATH_TO_PREPARED_TAP --remote origin --push
```

Both repositories use your existing GitHub CLI authentication. Do not put a GitHub token in a script or commit it.

## First release

The version in `pyproject.toml` and `delta_wifi/__init__.py` must match. After tests:

```sh
git tag v0.3.0
git push origin v0.3.0
```

GitHub Actions will test, build the Python wheel and universal helpers, verify a clean isolated installation, generate the formula/checksum, and create a **draft** release. Watch it with:

```sh
gh run list --workflow release.yml
gh run watch RUN_ID
```

Inspect the draft and build output. Then make it public:

```sh
gh release edit v0.3.0 --draft=false
```

A draft's assets are not anonymously downloadable by Homebrew, so publish before updating the tap. Then run the tap's update workflow:

```sh
gh workflow run update.yml --repo chiesennegs/homebrew-tap -f version=0.3.0
```

That workflow downloads the public release archive and generated formula, verifies the checksum/version/URL, and commits the recipe in the tap. Its own repository token can update the tap, so a separate cross-repository personal access token is unnecessary.

Once it succeeds, the install command above is live. Run `brew test delta-wifi` after installing. The first actual Homebrew installation is an additional validation beyond the isolated wheel/helper smoke test.

## Subsequent releases

1. Change the version in both files and add release notes in the commit/PR.
2. Merge to main after CI passes.
3. Push the corresponding `vX.Y.Z` tag.
4. Review and publish the generated draft release.
5. Dispatch `homebrew-tap`'s update workflow with the new version.
6. Users run `brew upgrade delta-wifi` after `brew update`.

A version tag should not be moved after publication. Fix an incorrect release with a new version. Keep old release assets available so recipes and rollbacks remain reproducible.

To return to an older known version for troubleshooting, use a separate isolated source invocation without changing the installed formula:

```sh
uvx --from 'git+https://github.com/chiesennegs/delta-wifi.git@v0.3.0' delta-wifi --version
```

This is not a Homebrew downgrade command; Homebrew normally follows the current tap formula.

## Signing status

The initial pipeline uses ad-hoc signatures and records this in `release-manifest.json`; it is not Developer ID signed or notarized. Both architectures and signatures are checked. Homebrew installation is the intended initial distribution route.

For a polished signed/notarized download, configure an Apple Developer ID certificate and notarization credentials, pass the signing identity to the builder, and notarize/staple helpers before archiving. Never commit certificates, private keys, or passwords. Developer ID continuity and stable paths improve permission continuity, but permission retention across upgrades still needs macOS validation. Ad-hoc helper updates may require reauthorizing Location Services or notifications. No security protection should be disabled to install the tool.

## Local release build

Requires macOS, Apple Command Line Tools, Python 3.11+, and build tooling:

```sh
python -m pip install build setuptools wheel
python scripts/package_release.py
python scripts/smoke_release.py dist/delta-wifi-0.3.0-macos-universal.tar.gz
python scripts/render_formula.py --repo chiesennegs/delta-wifi --archive dist/delta-wifi-0.3.0-macos-universal.tar.gz --output dist/delta-wifi.rb
```

Builds happen in temporary staging directories and do not re-sign or replace the currently authorized development helpers.
