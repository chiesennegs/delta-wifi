import unittest
from unittest.mock import Mock
from delta_wifi import cli
from delta_wifi.portal import PortalClient


class VideoSkipTests(unittest.TestCase):
    def client(self, buttons=0, divs=1):
        client = PortalClient(cli.PORTAL, cli.trusted, Mock(), Mock(), Mock(return_value=True))
        page = client.page = Mock(url=cli.PORTAL)
        button = page.get_by_role.return_value
        button.count.return_value = buttons
        div = page.locator.return_value.filter.return_value
        div.count.return_value = divs
        for candidate in (button, div):
            candidate.is_visible.return_value = True
            candidate.is_enabled.return_value = True
            candidate.evaluate.return_value = False
        return client, button, div

    def test_supplied_div_selector_is_clicked_once(self):
        client, button, div = self.client()
        self.assertTrue(client.try_video_skip())
        self.assertFalse(client.try_video_skip())
        client.page.locator.assert_called_once_with('div.content-container:visible')
        div.click.assert_called_once_with(timeout=1500)
        button.click.assert_not_called()

    def test_accessible_button_takes_precedence_over_nested_div(self):
        client, button, div = self.client(buttons=1)
        self.assertTrue(client.try_video_skip())
        button.click.assert_called_once()
        div.click.assert_not_called()

    def test_disabled_control_and_disabled_ancestor_are_not_clicked(self):
        client, button, div = self.client(buttons=1)
        button.is_enabled.return_value = False
        self.assertFalse(client.try_video_skip())
        button.click.assert_not_called()
        div.click.assert_not_called()
        client, button, div = self.client()
        div.evaluate.return_value = True
        self.assertFalse(client.try_video_skip())
        div.click.assert_not_called()

    def test_ambiguous_or_hidden_controls_are_not_clicked(self):
        for buttons, divs in ((2, 1), (0, 2), (0, 0)):
            client, button, div = self.client(buttons, divs)
            self.assertFalse(client.try_video_skip())
            button.click.assert_not_called()
            div.click.assert_not_called()
        client, button, div = self.client()
        div.is_visible.return_value = False
        self.assertFalse(client.try_video_skip())
        div.click.assert_not_called()

    def test_external_origin_never_receives_skip_click(self):
        client, button, div = self.client()
        client.page.url = 'https://unrecognized.example/'
        self.assertFalse(client.try_video_skip())
        client.page.locator.assert_not_called()
        client.page.get_by_role.assert_not_called()

    def test_overlay_failure_does_not_escape_or_mark_skip_successful(self):
        client, button, div = self.client()
        div.click.side_effect = RuntimeError('covered by another element')
        self.assertFalse(client.try_video_skip())
        self.assertFalse(client.video_skip_clicked)

    def test_ignored_click_can_be_retried_when_skip_later_becomes_actionable(self):
        client, button, div = self.client()
        div.wait_for.side_effect = [RuntimeError('still visible'), None]
        self.assertFalse(client.try_video_skip())
        self.assertFalse(client.video_skip_clicked)
        self.assertTrue(client.try_video_skip())
        self.assertEqual(div.click.call_count, 2)
