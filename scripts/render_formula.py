#!/usr/bin/env python3
"""Generate the tap recipe from the actual release asset and checksum."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import tarfile

TEMPLATE = '''class DeltaWifi < Formula
  include Language::Python::Virtualenv

  desc "Monitor and recover Delta inflight Wi-Fi from the command line"
  homepage "https://github.com/@@REPO@@"
  url @@URL@@
  version "@@VERSION@@"
  sha256 "@@SHA@@"
  license "MIT"

  depends_on :macos
  depends_on "python@3.13"

  def install
    venv = virtualenv_create(libexec, "python3.13")
    wheels = Dir["delta_wifi-*.whl"]
    odie "Expected exactly one application wheel" unless wheels.length == 1
    venv.pip_install_and_link wheels.first
    pkgshare.install "helpers"
    pkgshare.install "release-manifest.json"
    (bin/"delta-wifi").unlink
    (bin/"delta-wifi").write_env_script libexec/"bin/delta-wifi",
      DELTA_WIFI_BUNDLED_HELPERS: opt_pkgshare/"helpers"
  end

  def caveats
    <<~EOS
      Enable Wi-Fi name detection once:
        delta-wifi wifi-perms
      Optional alerts:
        delta-wifi notify-perms
      Credentials remain in macOS Keychain; preferences/history remain in your
      Application Support directory during upgrades and ordinary uninstalls.
      This formula installs HTTP mode. Browser mode is an optional Python extra.
    EOS
  end

  test do
    assert_match "delta-wifi @@VERSION@@", shell_output("#{bin}/delta-wifi --version")
    assert_match "logout", shell_output("#{bin}/delta-wifi --help")
  end
end
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', required=True, help='GitHub OWNER/REPO')
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--url', help='Override only for local installation tests')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', args.repo):
        parser.error('repo must be OWNER/REPO')
    with tarfile.open(args.archive) as archive:
        manifests = [entry for entry in archive.getmembers() if entry.name.endswith('/release-manifest.json')]
        if len(manifests) != 1:
            parser.error('archive must contain one release manifest')
        manifest = json.load(archive.extractfile(manifests[0]))
    if not args.url and set(manifest.get('architectures', [])) != {'arm64','x86_64'}:
        parser.error('public formula requires a universal release; use --url for local architecture-specific tests')
    version = manifest['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        parser.error('version must be X.Y.Z')
    checksum = hashlib.sha256(args.archive.read_bytes()).hexdigest()
    url = args.url or f'https://github.com/{args.repo}/releases/download/v{version}/{args.archive.name}'
    # Quote a Ruby string without allowing interpolation from a supplied test URL.
    ruby_url = json.dumps(url).replace('#{', '\\#{')
    result = TEMPLATE.replace('@@REPO@@',args.repo).replace('@@VERSION@@',version).replace('@@SHA@@',checksum).replace('@@URL@@',ruby_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(result)
    print(args.output)


if __name__ == '__main__':
    main()
