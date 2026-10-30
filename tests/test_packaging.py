import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from delta_wifi import helper_install, storage


class HelperUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.bundle = root/'bundled'
        self.state = root/'state'
        env = patch.dict(os.environ, DELTA_WIFI_BUNDLED_HELPERS=str(self.bundle),
                         DELTA_WIFI_STATE_DIR=str(self.state))
        env.start()
        self.addCleanup(env.stop)
        self.name = 'Delta WiFi Helper.app'
        self.app = self.bundle/self.name
        (self.app/'Contents'/'MacOS').mkdir(parents=True)
        (self.app/'Contents'/'Info.plist').write_text('fixture-plist')
        self.binary = self.app/'Contents'/'MacOS'/'DeltaWiFiHelper'
        self.binary.write_bytes(b'version-one')

    def test_upgrade_keeps_helper_path_and_user_state(self):
        storage.set_account('fixture-user')
        storage.record('connection', internet='online')
        first = helper_install.ensure(self.name)
        self.binary.write_bytes(b'version-two')
        second = helper_install.ensure(self.name)
        self.assertEqual(first, second)
        self.assertEqual((second/'Contents'/'MacOS'/'DeltaWiFiHelper').read_bytes(), b'version-two')
        self.assertEqual(storage.preferred_account(), 'fixture-user')
        self.assertEqual(storage.history()[0]['internet'], 'online')

    def test_unchanged_helpers_are_not_replaced_on_each_status_check(self):
        helper_install.ensure(self.name)
        with patch.object(helper_install.shutil, 'copytree') as copy:
            helper_install.ensure(self.name)
            copy.assert_not_called()

    def test_copy_failure_preserves_previous_install(self):
        target = helper_install.ensure(self.name)
        self.binary.write_bytes(b'next-version')
        with patch.object(helper_install.shutil, 'copytree', side_effect=OSError('disk failure')):
            with self.assertRaises(OSError):
                helper_install.ensure(self.name)
        self.assertEqual((target/'Contents'/'MacOS'/'DeltaWiFiHelper').read_bytes(), b'version-one')

    def test_source_symlinks_are_rejected(self):
        (self.app/'unexpected').symlink_to(self.binary)
        with self.assertRaisesRegex(RuntimeError, 'symbolic link'):
            helper_install.ensure(self.name)

    def test_source_installs_keep_original_location(self):
        with patch.dict(os.environ, DELTA_WIFI_BUNDLED_HELPERS=''):
            target = helper_install.ensure(self.name)
            self.assertEqual(target, Path(helper_install.__file__).parent/'native'/self.name)
