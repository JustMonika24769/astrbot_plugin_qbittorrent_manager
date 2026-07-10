import asyncio
import importlib
import sys
import types
import unittest


def _install_astrbot_stubs():
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    event_api = types.ModuleType("astrbot.api.event")
    star_api = types.ModuleType("astrbot.api.star")
    core = types.ModuleType("astrbot.core")
    core_star = types.ModuleType("astrbot.core.star")
    core_filter = types.ModuleType("astrbot.core.star.filter")
    command = types.ModuleType("astrbot.core.star.filter.command")

    class Logger:
        def exception(self, *_args, **_kwargs):
            pass

    class CommandGroup:
        def command(self, *_args, **_kwargs):
            return lambda handler: handler

    class Filter:
        def command(self, *_args, **_kwargs):
            return lambda handler: handler

        def command_group(self, *_args, **_kwargs):
            return lambda _handler: CommandGroup()

    class Star:
        def __init__(self, context):
            self.context = context

    class GreedyStr(str):
        pass

    api.logger = Logger()
    event_api.AstrMessageEvent = object
    event_api.filter = Filter()
    star_api.Context = object
    star_api.Star = Star
    star_api.register = lambda *_args, **_kwargs: lambda cls: cls
    command.GreedyStr = GreedyStr

    modules = {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_api,
        "astrbot.api.star": star_api,
        "astrbot.core": core,
        "astrbot.core.star": core_star,
        "astrbot.core.star.filter": core_filter,
        "astrbot.core.star.filter.command": command,
    }
    sys.modules.update(modules)


_install_astrbot_stubs()
plugin_module = importlib.import_module("main")


class SavingConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_count = 0

    def save_config(self):
        self.save_count += 1


class FakeEvent:
    def __init__(self, user_id):
        self.user_id = user_id

    def get_sender_id(self):
        return self.user_id


def make_plugin(config):
    instance = plugin_module.QBittorrentManagerPlugin.__new__(
        plugin_module.QBittorrentManagerPlugin
    )
    instance.config = config
    instance._config_lock = asyncio.Lock()
    instance._search_cache = {}
    return instance


class ParserTests(unittest.TestCase):
    def test_skips_outer_layout_row(self):
        html_text = """
        <table><tr><td>搜索箱（点击收起/展开）
          <table><tr>
            <td>动漫</td>
            <td><table class="torrentname"><tr><td>
              <a title="测试种子" href="details.php?id=1">测试种子</a><br>正确简介
              <a href="download.php?id=1">下载</a>
            </td></tr></table></td>
            <td>0</td><td>2026-01-01</td><td>1.5 GiB</td>
            <td>4</td><td>0</td><td>10</td><td>tester</td>
          </tr></table>
        </td></tr></table>
        """
        instance = make_plugin({})

        results = instance._parse_nexusphp_results(
            html_text, "https://example.com/", 10
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "测试种子")
        self.assertEqual(results[0].subtitle, "正确简介 下载")
        self.assertEqual(
            (results[0].seeders, results[0].leechers, results[0].completed),
            ("4", "0", "10"),
        )


class UserConfigTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = SavingConfig(
            {
                "download_client": "qbittorrent",
                "qb_url": "http://global:8080",
                "provider_cookie": "global-cookie",
                "user_profiles": [
                    {
                        "__template_key": "user",
                        "qq": "10001",
                        "download_client": "utorrent",
                        "ut_url": "http://user:8080",
                    }
                ],
            }
        )
        self.instance = make_plugin(self.config)

    def test_configuration_is_selected_by_qq(self):
        first = self.instance._user_config(FakeEvent("10001"))
        second = self.instance._user_config(FakeEvent("10002"))

        self.assertEqual(first["download_client"], "utorrent")
        self.assertEqual(first["ut_url"], "http://user:8080")
        self.assertEqual(second["download_client"], "qbittorrent")
        self.assertEqual(second["qb_url"], "http://global:8080")

    async def test_user_override_is_saved_and_can_be_deleted(self):
        await self.instance._set_user_override("10002", "qb_password", "secret-value")

        overrides = self.instance._user_overrides("10002")
        rendered = self.instance._format_user_config(
            "10002", self.instance._user_config(FakeEvent("10002")), overrides
        )
        self.assertEqual(overrides["qb_password"], "secret-value")
        self.assertNotIn("secret-value", rendered)
        self.assertIn("qBittorrent 密码：已设置（个人）", rendered)
        self.assertEqual(self.config.save_count, 1)

        removed = await self.instance._delete_user_override("10002", "qb_password")
        self.assertTrue(removed)
        self.assertNotIn("qb_password", self.instance._user_overrides("10002"))
        self.assertEqual(self.config.save_count, 2)

    def test_configuration_value_validation(self):
        self.assertEqual(
            self.instance._parse_user_config_value("download_client", "qb"),
            "qbittorrent",
        )
        self.assertTrue(self.instance._parse_user_config_value("qb_paused", "开启"))
        with self.assertRaises(plugin_module.UserFacingError):
            self.instance._parse_user_config_value("max_results", "31")

    def test_cookie_origin_check(self):
        self.assertTrue(
            self.instance._same_origin(
                "https://pt.example/download.php?id=1", "https://pt.example/"
            )
        )
        self.assertFalse(
            self.instance._same_origin(
                "https://files.example/item.torrent", "https://pt.example/"
            )
        )


if __name__ == "__main__":
    unittest.main()
