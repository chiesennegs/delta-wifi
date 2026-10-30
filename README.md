# delta-wifi

A macOS CLI for monitoring and recovering Delta inflight Wi-Fi. Login, retries, provider-policy checks, internet checks, and device logout use HTTP by default. No browser is launched unless you explicitly select browser mode or open the portal manually.

## Homebrew distribution

The Homebrew packaging and release workflow are being prepared. Once the first tagged release and tap recipe are published:

```sh
brew install chiesennegs/tap/delta-wifi
brew update
brew upgrade delta-wifi
```

This installs the HTTP core and precompiled native helpers without manual venv activation. See [the publishing guide](packaging/HOMEBREW.md) for repository setup, draft-release review, tap updates, signing status, and maintenance. The initial prebuilt helpers have development signatures; stable Developer ID signing/notarization is not yet configured.

## Setup from source

Requires Python 3.10+ and macOS. Default HTTP mode uses only Python's standard library.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
delta-wifi wifi-perms
delta-wifi credentials YOUR_ACCOUNT
delta-wifi watch --auto-login
```

Allow **Delta WiFi Helper** to use Location Services when prompted. Apple gates SSID access behind this permission; geographic coordinates are not requested. If denied, enable it in System Settings > Privacy & Security > Location Services. Source installations build the native helper on first use, requiring Apple Command Line Tools. Homebrew installations use precompiled helpers. Keep it in its generated location; rebuilding or moving it may require authorization again. It is separate from browser automation.

An editable installation picks up changes immediately. To select credentials saved by an older version without re-entering the password:

```sh
delta-wifi account use YOUR_ACCOUNT
```

## Browser-free login and recovery

```sh
delta-wifi login
delta-wifi login YOUR_ACCOUNT
delta-wifi watch --auto-login
delta-wifi watch --auto-login YOUR_ACCOUNT --max-attempts 3 --retry-delay 30
```

HTTP mode follows the observed production portal's Ping OAuth/PKCE flow, obtains an access token, and requests `freeWithSkyMiles` access for the current flight/device. It then checks both the provider's device policy and general-web access. This uses the portal's existing API, without DOM automation or browser cookies.

- TLS verification stays enabled; requests are restricted to the exact `wifi.delta.com` and `signin.delta.com` HTTPS origins.
- Flight/device identifiers are discovered live, never guessed or accepted from command-line arguments. The portal's device IP must match the laptop Wi-Fi IP before access changes.
- Credentials are read from Keychain only when the identity service requests username/password authentication. At most one password submission occurs within a flow.
- The portal's ordinary fresh-device information step is supported using a new ephemeral identifier. Stored fingerprints or other users' identities are never imitated.
- Unknown authentication states, MFA, verification challenges, unexpected redirects, and rejected credentials stop the flow. Browser mode is an explicit alternative; there is no automatic browser fallback.
- OAuth tokens, codes, flow/device identifiers, fingerprints, and cookies stay in memory and are never logged or persisted. Changing the account or leaving the network clears the identity context.
- A temporary failure activating access can retry using the existing in-memory token without resubmitting the password. A broken connection after a password submission with an unknown outcome pauses password retries.
- Existing premium access is preserved. If web checks fail while Delta still reports premium access, monitoring investigates the outage rather than repeatedly logging in.

Use `--force` to request authentication/activation despite an existing access policy. The network, origin, device-IP, rejection, and retry safeguards still apply.

The HTTP adapter is based on `main.c9a4c2346f166521.js` observed on 2026-10-30. These are undocumented provider interfaces and may change; unfamiliar schemas stop safely with an error. Browser mode remains available for unsupported flows.

## Device logout for repeat testing

```sh
delta-wifi logout --dry-run
delta-wifi logout
delta-wifi login YOUR_ACCOUNT
```

**Logout resets this laptop's onboard premium access to the portal's default/basic policy.** It does not invoke global SkyMiles logout, remove Keychain credentials, or change your Delta password. Success requires provider confirmation of a basic/default policy; losing a ping or failing a web check is not sufficient proof.

Run it while the laptop is connected to **DeltaWiFi.com**, not through the phone hotspot. Both the SSID and the server-reported device IP are checked to avoid changing the phone's session. `--dry-run` is read-only and does not pause recovery or submit a reset.

Stop any in-progress login before testing logout. Actual logout sets a shared local recovery-pause flag so existing watch loops do not immediately undo it. An already-submitted operation cannot be recalled. Running `login` or restarting `watch --auto-login` explicitly resumes recovery. Monitor-only watch does not clear the pause.

The reset POST is submitted once; it is not automatically retried if confirmation fails. Default verification time is 30 seconds, configurable with `--verify-timeout`. HTTP timeouts or an unrecognized policy produce an unconfirmed/error result rather than a false success. `--json` prints the result without flight/device identifiers.

A successful logout and subsequent fresh HTTP-only login were validated by the user on a Delta flight on 2026-10-30. The fresh login recovered from an initial connection timeout, completed OAuth and access activation, and confirmed `DalPedPremium` plus general-web connectivity.

## Optional browser mode

```sh
python -m pip install -e '.[browser]'
delta-wifi login --mode browser
delta-wifi login --mode browser --browser canary
delta-wifi watch --auto-login --mode browser --browser canary
delta-wifi portal-info --mode browser --browser canary
```

Production browser mode uses Google Chrome installed in `/Applications`; development can select Chrome Canary. It launches a separate temporary profile, without using your personal browser profile. `--browser` only selects the application; it does not enable browser mode. `--headless` applies only with `--mode browser`.

The browser adapter opens the observed SkyMiles dialog and submits its recognized fields. During a post-login video, it selects a unique visible, enabled **Skip** control: an accessible button first, then the observed `div.content-container` containing exactly “Skip.” Angular-generated attributes are not used. A click counts as effective only when the control disappears; an ignored early click can be retried later. It does not modify video positions, countdowns, disabled states, or hidden UI. No terms, CAPTCHAs, or verification challenges are automatically completed.

Both modes exit once their required access verification succeeds. HTTP mode does not launch or interact with a video UI. Browser mode can exit while a video remains if general-web access is already available. The default post-submit verification window is 90 seconds; individual network checks add time.

```sh
delta-wifi login --verify-timeout 120
delta-wifi login --mode browser --browser canary --verify-timeout 120
```

Browser login was validated by the user on a flight. Actual Skip-control handling has synthetic tests and still needs live validation.

## Monitoring, waiting, and terminal display

```sh
delta-wifi status
delta-wifi watch
delta-wifi watch --auto-login
delta-wifi watch --display log
delta-wifi watch --display panel
delta-wifi watch --json
```

Start watch before boarding if useful. It waits for `DeltaWiFi.com`, starts checks when the network appears, and returns to waiting when you leave. It does not join networks. Monitoring does not probe unrelated SSIDs.

A terminal automatically gets a compact updating panel: Wi-Fi, provider/session evidence, internet, gateway, DNS, route/VPN/proxy evidence, last verified web access, outage duration, failed recovery attempts, next action, and countdown. Ctrl+C restores the terminal and clears in-memory identity state. The countdown covers the interval between checks; check execution adds time.

Piped output uses scrolling text. `--json` always disables the panel and emits newline-delimited status, recovery, and next-check events. Last verified general-web access is retained across runs; older timestamps recorded under the weaker partial-access policy are ignored.

Retry defaults: up to three failed recovery attempts, with exponential backoff and jitter starting at 30 seconds. Rejected credentials, rate limits, challenges, and uncertain post-password outcomes pause attempts while monitoring continues. Three fully online observations reset a temporary attempt limit; errors requiring user action need resolution and a new explicit login/watch invocation.

## Saved account and Keychain

```sh
delta-wifi credentials YOUR_ACCOUNT
delta-wifi credentials
delta-wifi account
delta-wifi account use OTHER_ACCOUNT
delta-wifi account forget
delta-wifi account forget OTHER_ACCOUNT
```

The credentials command privately prompts for the password, updates its Keychain entry, and remembers the preferred account. Passwords are never passed as command-line arguments. Account inspection checks entry availability without reading the password. Forget removes local credentials and the matching preference only; it does not log out an onboard session.

`login` and `watch --auto-login` use the preferred account if ACCOUNT is omitted. An explicit account overrides it for that invocation. The preferred username is stored privately as a preference, not in event history.

## Connection history

```sh
delta-wifi history
delta-wifi history --limit 100 --json
delta-wifi history --clear
```

A private local SQLite database retains up to 5,000 events: connection changes, outage starts/ends/interruptions, recovery attempts, actual password submissions, results, durations, logout results, and watch starts/stops. Events exclude usernames, passwords, SSIDs, IP addresses, cookies, tokens, HTML, and request payloads.

State lives under `~/Library/Application Support/delta-wifi`, with a private directory and database permissions. `--clear` removes history while retaining account, last-access, and recovery-pause preferences. Latest events are displayed chronologically; JSON is an array. Leaving a network or stopping watch marks an ongoing outage as interrupted rather than recovered. `DELTA_WIFI_STATE_DIR` can isolate tests; use a directory dedicated to this tool because private permissions are applied to it.

## Notifications

```sh
delta-wifi notify-perms
delta-wifi watch --auto-login --notify
delta-wifi watch --auto-login --notify --notify-after 120
```

Allow **Delta WiFi Alerts** notification access. It is a separate helper and does not rebuild the Wi-Fi helper or change Location Services authorization. This checkout includes the compiled helper; new installations build it on first use.

Routine monitoring/retries stay quiet. An alert is queued once when login needs attention and once when an outage lasting at least 60 seconds (configurable) ends. Partial internet access never triggers recovery notification. A delivery failure does not stop monitoring. macOS Focus and notification settings control whether queued alerts are shown.

## Diagnostics and access evidence

```sh
delta-wifi portal-info
delta-wifi diagnose > diagnostic.json
delta-wifi connectivity --json > connection-sample.json
delta-wifi connectivity --samples 3 --json > repeated-samples.json
delta-wifi open-portal
```

Default `portal-info` is HTTP-only and reports safe provider-policy/adapter metadata. Browser mode instead reports login field counts and closes afterward. Neither reads Keychain or submits credentials. `open-portal` opens the system default browser for manual use.

`connectivity` works on any SSID, so it can compare a hotspot with unauthenticated Delta Wi-Fi. Reports contain endpoint HTTP status, timing, expected-content result, safe error class/errno, redirect hosts, SSID before/after, and route/VPN/proxy evidence. A change of SSID or local IP during a sample invalidates it. Bodies, public IPs from trace responses, cookies, request payloads, and query strings are excluded. Review SSID/interface names before sharing.

Access evidence is intentionally separate:

- `Session: premium-access` means Delta's device-policy API reports `DalPedPremium` for an IP matching this laptop. It does not identify the SkyMiles account or prove general internet access. A paid or other premium entitlement can use the same policy.
- `Session: login-required` means a known basic policy, form, or portal-redirect evidence suggests full access needs activation. `unknown` means evidence is insufficient.
- Internet `online` requires at least two general-web checks to pass, without a known Delta portal redirect. It is sampled access evidence, not proof every site is reachable.
- Internet `limited` means some destinations respond without enough general-web evidence. OS connectivity checks and ICMP can pass before authentication; they never establish success alone.
- `captive` means a redirect to a known Delta portal host was observed. `captive_evidence: false` does not mean authenticated: a restricted network may reset or time out HTTPS instead of redirecting it.
- General-web checks use Cloudflare, Wikipedia, and Python.org (a small robots.txt response). Apple/Google OS checks are reported separately. Fresh URLs, no-cache headers, expected content/status, and bounded same-origin HTTPS redirects reduce ambiguity. Normal canonical redirects are not captive evidence.

Network diagnostics include Wi-Fi association, local IPv4 address, scoped gateway/ICMP, system DNS, representative IPv4/IPv6 routes, connected VPN indicators, and system/environment proxy configuration. A gateway ping reply confirms only that reply; no reply may indicate ICMP filtering. DNS `working` means all sampled names resolve; `partial` means some; `failed` means none. A detected VPN service alone does not prove a sampled request used its tunnel.

A route snapshot does not bind application traffic to Wi-Fi and cannot detect every split-tunnel or per-app VPN behavior. Alternate routes or a configured proxy suspend automatic login rather than producing a misleading success. If external DNS fails but the portal responds, login can still be attempted. The tool never changes network settings, reconnects Wi-Fi, or disables VPNs.

If macOS hides the SSID, confirm DeltaWiFi.com in the Wi-Fi menu before using `--assume-delta`. It requires a Wi-Fi IP, cannot override a known different SSID, and never bypasses the portal device-IP check. Stop watch before changing networks with that override.

## Testing and live validation

```sh
python -m unittest discover -s tests -v
DELTA_WIFI_BROWSER_TESTS=1 python -m unittest discover -s tests -v
```

Regular tests use synthetic HTTP OAuth/access-reset responses, network states, sanitized onboard captures, temporary storage, and mocked Keychain/notification operations. They cover device binding, PKCE, unsupported challenges, no credential redirects, token reuse after activation errors, uncertain password POSTs, recovery suspension after logout, and cleanup on leaving/rejoining a network.

The user validated browser login, device logout, and a fresh HTTP-only login with a transient-failure retry on 2026-10-30. General-web classification was checked against working hotspot and unauthenticated Delta captures. Live behavior on every aircraft/provider is not assumed.

Opt-in browser tests launch Canary against synthetic intercepted pages with fake credentials, making no Delta requests. The development environment aborts separate Chrome/Canary launches during macOS app registration, so run these from your own Terminal. If a browser quits before opening a page, the adapter pauses; do not repeatedly relaunch it from the agent environment. No browser security changes are required.

The notification helper compiles/signs successfully; its permission prompt and delivery still need user validation. The original Wi-Fi helper remains unchanged.
