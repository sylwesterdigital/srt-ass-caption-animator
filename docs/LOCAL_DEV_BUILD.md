Use the builder directly, not the release workflow:

```bash
APP_NAME="Cut Test" \
APP_SAFE_NAME="CutTest" \
BUNDLE_ID="fun.workwork.cut.test" \
BUILD_NUMBER_OVERRIDE="$(date +%y%m%d%H%M)" \
PERSIST_BUILD_NUMBER=0 \
MACOS_SIGN_IDENTITY="" \
NOTARY_PROFILE="" \
./build_macos_release_v3.sh
```

This:

* Builds a separate **Cut Test.app**
* Does not publish to GitHub
* Does not deploy the homepage
* Does not modify `BUILD_NUMBER.txt`
* Does not require a manual version bump
* Uses separate application storage: `~/Library/Application Support/Cut Test`
* Uses ad-hoc signing, suitable for local testing

Launch the result:

```bash
open ".macos-build/dist/Cut Test.app"
```

Or replace the previously opened test instance:

```bash
pkill -x "Cut Test" 2>/dev/null || true
open ".macos-build/dist/Cut Test.app"
```

The builder will still create local test ZIP/DMG files under `release/`, but nothing is uploaded or deployed.
