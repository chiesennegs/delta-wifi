#!/usr/bin/env python3
"""Install a built wheel into a clean environment and stage helpers without launching them."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='delta-wifi-smoke-') as temporary:
        root = Path(temporary)
        with tarfile.open(args.archive) as archive:
            for entry in archive.getmembers():
                if entry.name.startswith('/') or '..' in Path(entry.name).parts or entry.issym() or entry.islnk():
                    raise RuntimeError('Unsafe archive entry')
            archive.extractall(root)
        package = next(root.glob('delta-wifi-*'))
        wheels = list(package.glob('*.whl'))
        assert len(wheels) == 1
        environment = root/'venv'
        subprocess.run([sys.executable,'-m','venv',str(environment)],check=True)
        python = environment/'bin/python'
        subprocess.run([str(python),'-m','pip','install','--no-index','--no-deps',str(wheels[0])],check=True,cwd=root)
        env = {**os.environ,'DELTA_WIFI_STATE_DIR':str(root/'state'),
               'DELTA_WIFI_BUNDLED_HELPERS':str(package/'helpers')}
        subprocess.run([str(environment/'bin/delta-wifi'),'--version'],check=True,cwd=root,env=env)
        subprocess.run([str(python),'-c',
            'from delta_wifi import helper_install; '
            '[helper_install.ensure(name) for name in helper_install.NAMES]; '
            'print("Both prebuilt helpers installed; no compilation or UI launch.")'],
            check=True,cwd=root,env=env)
        for app in (root/'state'/'helpers').glob('*.app'):
            subprocess.run(['codesign','--verify','--deep','--strict',str(app)],check=True)


if __name__ == '__main__':
    main()
