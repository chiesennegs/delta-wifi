# delta-wifi

## Summary

**Spend less time wrestling with Delta Wi-Fi login.** `delta-wifi` is a macOS command-line tool that logs in with your SkyMiles account, retries temporary failures, and shows three distinct states: connection to the Wi-Fi network, the device's onboard access session, and actual internet connectivity.

The default mode uses HTTP directly, so it does not launch a browser. Passwords stay in macOS Keychain. Monitoring can wait for Delta Wi-Fi, recover when login is needed, and preserve an active session when the aircraft's internet connection is the problem.

The [first Homebrew pre-release](https://github.com/chiesennegs/delta-wifi/releases/tag/v0.3.0) is available for Apple Silicon and Intel Macs. This is an independent tool, not an official Delta product. It cannot repair an aircraft's upstream connection.

## Basic Usage

After [installing](#install), join **DeltaWiFi.com** using the macOS Wi-Fi menu, then:

```sh
# One-time permission to read the Wi-Fi network name.
delta-wifi wifi-perms

# One-time credential setup; prompts privately for your password.
delta-wifi credentials YOUR_SKYMILES_NUMBER_OR_USERNAME

# Log in now, without opening a browser.
delta-wifi login

# Keep checking and recover when login is needed. Ctrl+C stops it.
delta-wifi watch --auto-login
```

When macOS prompts, allow **Delta WiFi Helper** to use Location Services. macOS requires that permission to expose the connected network name; the helper does not request geographic coordinates.

Already saved credentials with an earlier version? Select that Keychain entry instead of entering the password again:

```sh
delta-wifi account use YOUR_SKYMILES_NUMBER_OR_USERNAME
```

Use `delta-wifi status` for a single connection snapshot. Use `delta-wifi --help` or `delta-wifi COMMAND --help` for command descriptions, options, and examples.

## Install

### Homebrew (recommended)

```sh
brew install chiesennegs/tap/delta-wifi
delta-wifi --version
```

Homebrew manages Python and its isolated environment. Native helpers are precompiled for Apple Silicon and Intel; you do not need to activate a venv or compile Swift on first use. The package installs the HTTP core; [browser mode](#optional-browser-mode) is an optional Python extra.

If migrating from an active development venv, run `deactivate` first. Your existing Keychain credentials, account preference, and history remain separate from the Homebrew installation. Run `delta-wifi wifi-perms` once after installation to authorize the installed helper.

To upgrade:

```sh
brew update
brew upgrade delta-wifi
```

The initial helpers use development signatures. Developer ID signing/notarization is not yet configured, so an upgrade may require reauthorizing Location Services or notifications.

### From Source

Requires macOS, Python 3.10+, Git, and Apple Command Line Tools for building the native helpers. Default HTTP mode uses only Python's standard library.

```sh
git clone https://github.com/chiesennegs/delta-wifi.git
cd delta-wifi
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Continue with [Basic Usage](#basic-usage). An editable installation picks up source changes immediately. To update a source installation:

```sh
git pull --ff-only
python -m pip install -e .
```

For source installs, the Wi-Fi and notification helpers build on first permission setup. Keep them in their generated locations; rebuilding or moving them may require authorization again.

## Examples

### Check, log in, or monitor

```sh
delta-wifi status                        # One snapshot.
delta-wifi login                         # Use the preferred saved account.
delta-wifi login OTHER_ACCOUNT           # Use another saved Keychain account.
delta-wifi watch                         # Monitor without submitting credentials.
delta-wifi watch --auto-login             # Monitor and recover automatically.
delta-wifi watch --display log            # Scrolling text instead of the live panel.
delta-wifi watch --json                   # Newline-delimited JSON for scripts.
```

You can start watch before boarding. It waits for DeltaWiFi.com, starts checks when the network appears, and returns to waiting when you leave. It does not join networks or probe unrelated SSIDs.

### Enable quiet notifications

```sh
delta-wifi notify-perms
delta-wifi watch --auto-login --notify
```

Allow **Delta WiFi Alerts** when prompted. Notifications are queued when login needs attention or a sustained outage ends; routine retries stay quiet. Change the minimum outage duration with `--notify-after 120` (default: 60 seconds).

### Review connection history

```sh
delta-wifi history
delta-wifi history --limit 100 --json
delta-wifi history --clear
```

### Diagnose an ambiguous connection

```sh
delta-wifi portal-info
delta-wifi connectivity --samples 3 --json > connection-samples.json
delta-wifi diagnose > diagnostic.json
delta-wifi open-portal
```

`connectivity` works on any SSID, so you can compare a phone hotspot with Delta Wi-Fi. `portal-info` and the diagnostic commands do not read passwords or submit credentials. `open-portal` opens the system default browser for manual use. Review network/interface names before sharing diagnostics.

### Log out and test a fresh login

Stop any in-progress login, stay connected to DeltaWiFi.com, then:

```sh
delta-wifi logout --dry-run               # Read-only device/IP checks.
delta-wifi logout                         # Reset this laptop's onboard access.
delta-wifi login                          # Authenticate again using HTTP.
```

Logout keeps Keychain credentials and resets only the matching laptop's onboard access, not your global Delta account. Do not run it through a phone hotspot: that is a different onboard connection, and the tool refuses it. Logout pauses existing recovery loops; `login` or restarting `watch --auto-login` explicitly resumes recovery.

## Advanced

### How access and connectivity are distinguished

A successful ping or OS connectivity check does **not** prove a usable internet connection or an authenticated session. Captive portals can allow selected traffic before login.

| Signal | Meaning |
| --- | --- |
| `Session: premium-access` | Delta's device-policy API reports `DalPedPremium` for an IP matching this laptop. It does not identify the SkyMiles account or prove internet access; other premium entitlements can use the same policy. |
| `Session: login-required` | A known basic policy, form, or portal redirect suggests full access needs activation. |
| `Session: unknown` | There is insufficient session evidence. |
| `Internet: online` | At least two general-web checks passed, without a known Delta portal redirect. This is sampled access evidence, not proof every site works. |
| `Internet: limited` | Some destinations respond, but there is insufficient general-web evidence. This never skips login inspection or announces recovery. |
| `Internet: captive` | A redirect to a known Delta portal host was observed. |
| `Internet: unavailable` | None of the sampled destinations passed. |
| `Internet: network-changed` | The SSID or local IP changed during the checks; the sample was discarded. |

General-web checks use Cloudflare, Wikipedia, and Python.org; Apple/Google OS checks are reported separately. Requests use fresh URLs, no-cache headers, and expected status/content checks. Up to three same-origin HTTPS redirects are followed. A normal canonical redirect is not captive evidence. `captive_evidence: false` does not mean authenticated: a restricted network may reset or time out HTTPS instead of redirecting it.

Network diagnostics include local IPv4 address, scoped gateway/ICMP, system DNS, representative IPv4/IPv6 routes, connected VPN indicators, and proxy configuration. A gateway reply confirms only ICMP reachability; no reply can mean filtering. DNS `working` means all sampled names resolve, `partial` means some do, and `failed` means none do.

A detected VPN service does not prove a sampled request used its tunnel. Route snapshots do not bind traffic to Wi-Fi or detect every per-app/split-tunnel behavior. Alternate routes or configured proxies suspend automatic login rather than producing a misleading success. External DNS failures do not prevent login if the portal remains reachable. The tool never changes network settings, reconnects Wi-Fi, or disables a VPN.

### HTTP authentication and retry behavior

The HTTP adapter follows the observed production portal's Ping OAuth/PKCE flow, obtains an access token, and requests `freeWithSkyMiles` access for the current flight/device. Both the provider policy and general-web access are then verified.

- TLS verification stays enabled. Requests are restricted to the exact `wifi.delta.com` and `signin.delta.com` HTTPS origins.
- Flight/device identifiers are discovered live, not guessed or supplied on the command line. The server-reported device IP must match the laptop Wi-Fi IP before access changes.
- Keychain is read only when the identity service requests credentials. At most one password submission occurs within a flow.
- The ordinary fresh-device information step uses a new ephemeral identifier; stored fingerprints or other users' identities are not imitated.
- OAuth tokens, codes, cookies, fingerprints, and flow/device identifiers stay in memory. They are not logged or persisted; leaving the network or changing accounts clears the identity context.
- Unknown states, MFA, verification challenges, unexpected redirects, and rejected credentials stop the flow. There is no automatic browser fallback.
- Default recovery limit: three failed attempts, with exponential backoff and jitter starting at 30 seconds. A temporary activation failure can reuse an in-memory token without resubmitting the password.
- A broken connection after a password submission with an unknown outcome pauses password retries. Three fully online observations reset a temporary attempt limit; user-action errors require resolving the issue and starting login/watch again.
- Existing premium access is preserved during upstream outages rather than repeatedly reauthenticated.

```sh
delta-wifi watch --auto-login --max-attempts 3 --retry-delay 30 --interval 20
delta-wifi login --verify-timeout 120
delta-wifi login --force
```

`--force` requests login/activation even when access already exists; it retains network, origin, device-IP, rejection, and retry safeguards. The default post-submit verification window is 90 seconds; individual network checks add time.

These are undocumented provider interfaces, based on `main.c9a4c2346f166521.js` observed on 2026-10-30. They may change. Unfamiliar schemas stop with an error; browser mode is an explicit alternative for unsupported flows.

### Optional browser mode

The Homebrew package installs the HTTP core. To invoke the browser extra in an automatically managed environment, using [uv](https://docs.astral.sh/uv/):

```sh
uvx --from 'delta-wifi[browser] @ git+https://github.com/chiesennegs/delta-wifi.git@v0.3.0' delta-wifi login --mode browser --browser canary
```

For a source/development installation:

```sh
python -m pip install -e '.[browser]'
delta-wifi login --mode browser
delta-wifi login --mode browser --browser canary
delta-wifi watch --auto-login --mode browser --browser canary
delta-wifi portal-info --mode browser --browser canary
```

Browser mode uses Google Chrome in `/Applications`, or Chrome Canary when selected. It launches a separate temporary profile. `--browser` chooses the application but does not enable browser mode; `--headless` applies only with `--mode browser`.

The adapter submits the recognized SkyMiles dialog. During a post-login video, it can select a unique visible, enabled **Skip** button or the observed `div.content-container` containing exactly “Skip.” It does not rely on Angular-generated attributes. An ignored click can be retried later; video positions, countdowns, disabled states, and hidden UI are not modified. Terms, CAPTCHAs, and verification challenges are not automatically completed.

Both modes exit when their required verification succeeds. HTTP mode never loads a video UI. Browser mode can exit while video remains if general-web access is available. Actual Skip handling still needs live validation.

### Saved accounts, private state, and history

```sh
delta-wifi credentials                   # Update the preferred account's password.
delta-wifi account                       # Inspect account/Keychain entry availability.
delta-wifi account use OTHER_ACCOUNT     # Select an existing entry without reading its password.
delta-wifi account forget                # Remove the preferred local entry/preference.
delta-wifi account forget OTHER_ACCOUNT
```

Passwords are never passed as command-line arguments. Account inspection does not read them. An explicit account overrides the preference for that invocation. Forget removes local credentials and the matching preference; it does not log out an onboard session.

Preferences and history live under `~/Library/Application Support/delta-wifi`, with private directory/database permissions. The preferred username is stored as a preference, not in event history. History retains up to 5,000 state changes, outage events, recovery attempts, password-submission events, results, durations, logout results, and watch starts/stops. Events exclude usernames, passwords, SSIDs, IPs, cookies, tokens, HTML, and request payloads.

`history --clear` retains account, last-access, and recovery-pause preferences. Durations use a monotonic clock; leaving a network or stopping watch marks an outage as interrupted rather than recovered. Last verified access survives restarts; timestamps from the older partial-access policy are ignored. `DELTA_WIFI_STATE_DIR` can isolate tests; use a dedicated directory because the tool applies private permissions to it.

The terminal panel updates in place. Piped output uses scrolling text. JSON emits newline-delimited status, recovery, and next-check events. Ctrl+C restores the terminal and clears in-memory identity state. Countdown time excludes check execution.

### Logout and notification details

Logout sends one reset request and requires provider confirmation of basic/default policy. Failed web checks alone do not establish logout. The reset is not automatically retried if confirmation fails. Default verification time is 30 seconds, configurable with `--verify-timeout`; `--json` excludes device/flight identifiers. An already-submitted login cannot be recalled, so stop in-progress login work before testing logout.

The notification helper is separate from Wi-Fi authorization. Homebrew supplies it precompiled; source installations build it on first permission setup. Brief outages and routine retries stay quiet. Partial access never triggers a recovery notification. Notification failures do not stop monitoring; macOS Focus and notification settings control whether queued alerts are displayed.

### Permissions and troubleshooting

If Wi-Fi authorization was denied, enable **Delta WiFi Helper** in System Settings > Privacy & Security > Location Services. If macOS still hides the SSID, first confirm DeltaWiFi.com in the Wi-Fi menu, then use `--assume-delta`. It requires a Wi-Fi IP, cannot override a known different SSID, and never bypasses the server device-IP check. Stop watch before changing networks with that override.

Default `portal-info` reports safe provider-policy/adapter metadata over HTTP. Browser mode instead reports login field counts and closes. Diagnostics do not read Keychain or submit credentials. `diagnose` includes local addresses and network/interface names; review before sharing. Connectivity reports omit bodies, public IPs from trace responses, cookies, request payloads, and query strings.

If a browser quits before opening a page, the adapter pauses. The development environment has aborted separate Chrome/Canary processes during macOS application registration; run browser tests from your own Terminal rather than repeatedly relaunching from the agent environment. No browser security settings need changing.

### Development, validation, and releases

```sh
python -m unittest discover -s tests -v
DELTA_WIFI_BROWSER_TESTS=1 python -m unittest discover -s tests -v
```

Regular tests use synthetic HTTP flows, network states, sanitized onboard captures, temporary storage, and mocked Keychain/notification operations. Browser tests use intercepted synthetic pages with fake credentials, making no Delta requests.

Browser login, device logout, and fresh HTTP-only login with a transient-failure retry were validated on a Delta flight on 2026-10-30. Classification was checked against working-hotspot and unauthenticated-Delta captures. This does not establish behavior on every aircraft/provider. Notification helpers compile/sign successfully; their permission prompt and delivery still need user validation.

For publishing, see the [Homebrew release guide](packaging/HOMEBREW.md). It covers version tags, GitHub's draft-release build, checksums, the [tap](https://github.com/chiesennegs/homebrew-tap), recipe updates, and signing status. CI verifies the universal helpers, an isolated installation, and an actual Homebrew installation before drafting a release.
