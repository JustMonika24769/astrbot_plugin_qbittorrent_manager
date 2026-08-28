import asyncio
import importlib
import inspect
import sys
import time
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
    def __init__(self, user_id, *, admin=False):
        self.user_id = user_id
        self.admin = admin

    def get_sender_id(self):
        return self.user_id

    def is_admin(self):
        return self.admin

    def plain_result(self, message):
        return message


def make_plugin(config):
    instance = plugin_module.QBittorrentManagerPlugin.__new__(
        plugin_module.QBittorrentManagerPlugin
    )
    instance.config = config
    instance._config_lock = asyncio.Lock()
    instance._search_cache = {}
    return instance


def make_results(count):
    return [
        plugin_module.TorrentResult(
            index=index,
            title=f"种子 {index}",
            subtitle="",
            size="1 GiB",
            seeders="1",
            leechers="0",
            completed="1",
            detail_url=f"https://example.com/details.php?id={index}",
            download_url=f"https://example.com/download.php?id={index}",
            added_at=f"2026-01-{index:02d} 12:00:00",
        )
        for index in range(1, count + 1)
    ]


class ParserTests(unittest.TestCase):
    def test_skips_outer_layout_row(self):
        html_text = """
        <table><tr><td>搜索箱（点击收起/展开）
          <table><tr>
            <td>动漫</td>
            <td><table class="torrentname"><tr><td>
              <a title="测试种子 2030-12-31" href="details.php?id=1">测试种子 2030-12-31</a><br>正确简介
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
        self.assertEqual(results[0].title, "测试种子 2030-12-31")
        self.assertEqual(results[0].subtitle, "正确简介 下载")
        self.assertEqual(
            (results[0].seeders, results[0].leechers, results[0].completed),
            ("4", "0", "10"),
        )
        self.assertEqual(results[0].added_at, "2026-01-01")
        self.assertEqual(results[0].status, "")
        self.assertIsNone(results[0].progress)

    def test_extracts_seeding_status_and_progress(self):
        html_text = """
        <table><tr>
          <td>动漫</td>
          <td><table class="torrentname">
            <tr><td><a title="测试种子" href="details.php?id=2">测试种子</a>
              <a href="download.php?id=2">下载</a></td></tr>
            <tr><td><img src="s_up.gif"></td><td>
              <div class="probar_a2" title="已下载，正在做种">
                <div class="probar_b2" style="width:100%"></div>
              </div>
            </td></tr>
          </table></td>
          <td>0</td><td>2026-07-25 12:00:00</td><td>2 GiB</td>
          <td>8</td><td>0</td><td>20</td><td>tester</td>
        </tr></table>
        """
        instance = make_plugin({})

        results = instance._parse_nexusphp_results(
            html_text, "https://example.com/", 10
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "做种")
        self.assertEqual(results[0].progress, 100.0)
        self.assertEqual(results[0].status_kind, "seeding")

    def test_extracts_downloading_status_and_decimal_progress(self):
        html_text = """
        <table><tr>
          <td>电影</td>
          <td><table class="torrentname">
            <tr><td><a title="下载中的种子" href="details.php?id=3">下载中的种子</a>
              <a href="download.php?id=3">下载</a></td></tr>
            <tr><td></td><td><div class="probar_a1" title="正在下载">
              <div class="probar_b1" style="width:42.5%"></div>
            </div></td></tr>
          </table></td>
          <td>0</td><td>2026-07-25</td><td>3 GiB</td>
          <td>5</td><td>1</td><td>9</td><td>tester</td>
        </tr></table>
        """
        instance = make_plugin({})

        result = instance._parse_nexusphp_results(
            html_text, "https://example.com/", 10
        )[0]

        self.assertEqual(result.status, "下载")
        self.assertEqual(result.progress, 42.5)
        self.assertEqual(result.status_kind, "downloading")

    def test_inactive_seeding_text_is_not_treated_as_active(self):
        instance = make_plugin({})
        status, kind = instance._normalize_torrent_status(
            "已下载，停止做种", "probar_a3"
        )

        self.assertEqual(status, "暂停")
        self.assertEqual(kind, "inactive")

    def test_missing_progress_is_not_rendered_as_zero_percent(self):
        instance = make_plugin({})
        result = make_results(1)[0]
        result.status = "暂停"
        result.status_kind = "inactive"

        rendered = instance._status_html(result)

        self.assertIn('class="status-label">暂停</span>', rendered)
        self.assertIn('class="progress-value">—</span>', rendered)
        self.assertNotIn("0%</span>", rendered)

    def test_unrelated_number_in_status_title_is_not_progress(self):
        soup = plugin_module.BeautifulSoup(
            '<div class="probar_a3" title="已下载 2 天，停止做种"></div>',
            "html.parser",
        )

        progress = make_plugin({})._extract_progress_percent(soup.div)

        self.assertIsNone(progress)

    def test_render_height_includes_each_status_row_without_clipping(self):
        instance = make_plugin({})
        results = make_results(30)
        for result in results:
            result.status = "做种"
            result.progress = 100.0

        width, height = instance._render_dimensions(results, {"render_width": 900})

        self.assertEqual(width, 900)
        self.assertEqual(height, 4594)


class SearchSortTests(unittest.TestCase):
    def setUp(self):
        self.instance = make_plugin({})

    def test_legacy_search_treats_entire_query_as_keyword(self):
        request = self.instance._parse_search_request(
            "[VCB-Studio] title --sort=seeders"
        )

        self.assertEqual(request.keyword, "[VCB-Studio] title --sort=seeders")
        self.assertIsNone(request.sort_by)

    def test_standalone_dashes_inside_legacy_keyword_are_not_options(self):
        request = self.instance._parse_search_request("Movie -- Director's Cut")

        self.assertEqual(request.keyword, "Movie -- Director's Cut")
        self.assertIsNone(request.sort_by)

    def test_option_like_keyword_can_be_escaped(self):
        request = self.instance._parse_search_request("-- --sort=seeders special title")

        self.assertEqual(request.keyword, "--sort=seeders special title")
        self.assertIsNone(request.sort_by)

    def test_options_are_separated_from_keyword(self):
        request = self.instance._parse_search_request(
            "--排序=做种 --顺序=升序 -- [VCB-Studio] 漆黑 = 子弹 -- final"
        )

        self.assertEqual(request.keyword, "[VCB-Studio] 漆黑 = 子弹 -- final")
        self.assertEqual(request.sort_by, "seeders")
        self.assertFalse(request.descending)

    def test_search_handler_uses_required_greedy_string(self):
        parameter = inspect.signature(self.instance.search_torrents).parameters["query"]

        self.assertIs(parameter.annotation, plugin_module.GreedyStr)
        self.assertIs(parameter.default, inspect.Parameter.empty)

    def test_fullwidth_chinese_options_are_supported(self):
        request = self.instance._parse_search_request(
            "－－排序＝做种 －－顺序＝降序 －－ 碧蓝航线"
        )

        self.assertEqual(request.keyword, "碧蓝航线")
        self.assertEqual(request.sort_by, "seeders")
        self.assertTrue(request.descending)

    def test_chinese_em_dash_options_are_supported(self):
        request = self.instance._parse_search_request(
            "——排序＝完成 ——顺序＝升序 —— 碧蓝航线"
        )

        self.assertEqual(request.keyword, "碧蓝航线")
        self.assertEqual(request.sort_by, "completed")
        self.assertFalse(request.descending)

    def test_single_unicode_dash_options_are_supported(self):
        request = self.instance._parse_search_request(
            "—排序＝时间 —顺序＝降序 — 碧蓝航线"
        )

        self.assertEqual(request.keyword, "碧蓝航线")
        self.assertEqual(request.sort_by, "time")
        self.assertTrue(request.descending)

    def test_unknown_sort_field_is_rejected(self):
        with self.assertRaisesRegex(plugin_module.UserFacingError, "不支持的排序字段"):
            self.instance._parse_search_request("--sort=热度 -- 关键词")

    def test_sort_query_replaces_existing_provider_order(self):
        url = self.instance._apply_search_sort(
            "https://pt.example/torrents.php?search=test&sort=4&type=asc",
            "seeders",
            True,
        )

        self.assertIn("search=test", url)
        self.assertIn("sort=7", url)
        self.assertIn("type=desc", url)
        self.assertEqual(url.count("sort="), 1)

    def test_numeric_results_are_sorted_and_reindexed(self):
        results = make_results(3)
        results[0].seeders = "2"
        results[1].seeders = "10"
        results[2].seeders = "?"

        sorted_results = self.instance._sort_results(results, "seeders", True)

        self.assertEqual(
            [result.title for result in sorted_results], ["种子 2", "种子 1", "种子 3"]
        )
        self.assertEqual([result.index for result in sorted_results], [1, 2, 3])


class MTeamTests(unittest.TestCase):
    def setUp(self):
        self.instance = make_plugin({})

    def test_mteam_provider_requires_token(self):
        with self.assertRaisesRegex(plugin_module.UserFacingError, "API Token"):
            self.instance._provider_config({"provider_type": "mteam"})

    def test_mteam_provider_uses_api_defaults(self):
        provider = self.instance._provider_config(
            {"provider_type": "mteam", "mteam_api_token": "token"}
        )

        self.assertEqual(provider["type"], "mteam")
        self.assertEqual(provider["base_url"], "https://api.m-team.cc")
        self.assertEqual(provider["search_path"], "/api/torrent/search")
        self.assertEqual(provider["download_path"], "/api/torrent/genDlToken")

    def test_mteam_result_payload_is_parsed(self):
        payload = {
            "code": 0,
            "data": {
                "data": [
                    {
                        "id": 42,
                        "name": "M-Team 测试种子",
                        "size": 1073741824,
                        "seeders": 8,
                        "leechers": 2,
                        "timesCompleted": 10,
                        "createdDate": "2026-08-27 12:00:00",
                    }
                ]
            },
        }

        items = self.instance._mteam_items(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual(self.instance._format_mteam_size(items[0]["size"]), "1 GiB")

    def test_mteam_download_url_is_extracted_from_wrapped_payload(self):
        self.assertEqual(
            self.instance._mteam_download_url(
                {"code": 0, "data": {"url": "https://dl.example/test.torrent"}}
            ),
            "https://dl.example/test.torrent",
        )

    def test_mteam_provider_alias_is_accepted(self):
        self.assertEqual(
            self.instance._parse_user_config_value("provider_type", "m-team"),
            "mteam",
        )

    def test_mteam_sort_field_uses_official_enum(self):
        self.assertEqual(plugin_module.MTEAM_SORT_FIELDS["completed"], "TIMES_COMPLETED")

    def test_mteam_stats_are_read_from_nested_status_without_leaking_metadata(self):
        item = {
            "name": "真实标题",
            "status": {
                "seeders": 12,
                "leechers": 3,
                "timesCompleted": 8,
                "toppingLevel": 1,
                "status": "NORMAL",
                "modifiedDate": "2026-08-28T00:00:00",
            },
        }
        status = item["status"]

        self.assertEqual(
            self.instance._mteam_display_value(status, item, "seeders"), "12"
        )
        self.assertEqual(
            self.instance._mteam_display_value(status, item, "leechers"), "3"
        )
        self.assertEqual(
            self.instance._mteam_display_value(status, item, "timesCompleted"), "8"
        )
        self.assertEqual(
            self.instance._mteam_display_value(status, item, "unknown"), "?"
        )

    def test_mteam_zero_stats_are_preserved(self):
        status = {"seeders": 0, "leechers": 0, "timesCompleted": 0}
        item = {}

        self.assertEqual(
            self.instance._mteam_display_value(status, item, "seeders"), "0"
        )
        self.assertEqual(
            self.instance._mteam_display_value(status, item, "leechers"), "0"
        )
        self.assertEqual(
            self.instance._mteam_display_value(status, item, "timesCompleted"), "0"
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
            "10002",
            self.instance._user_config(FakeEvent("10002")),
            overrides,
            True,
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


class AccessControlTests(unittest.TestCase):
    def test_blacklist_takes_priority_for_regular_users(self):
        instance = make_plugin(
            {
                "access_whitelist": ["10001"],
                "access_blacklist": ["10001"],
                "access_admin_bypass": False,
            }
        )

        with self.assertRaisesRegex(plugin_module.UserFacingError, "黑名单"):
            instance._ensure_user_access(FakeEvent("10001"))

    def test_nonempty_whitelist_rejects_other_users(self):
        instance = make_plugin({"access_whitelist": ["10001"]})

        instance._ensure_user_access(FakeEvent("10001"))
        with self.assertRaisesRegex(plugin_module.UserFacingError, "白名单"):
            instance._ensure_user_access(FakeEvent("10002"))

    def test_admin_can_bypass_access_lists(self):
        instance = make_plugin(
            {
                "access_whitelist": ["10001"],
                "access_blacklist": ["90001"],
                "access_admin_bypass": True,
            }
        )

        instance._ensure_user_access(FakeEvent("90001", admin=True))

    def test_global_config_can_be_limited_to_admins(self):
        instance = make_plugin(
            {
                "global_config_admin_only": True,
                "qb_url": "http://global:8080",
                "provider_cookie": "global-cookie",
                "user_profiles": [
                    {
                        "__template_key": "user",
                        "qq": "10001",
                        "qb_username": "personal-user",
                    }
                ],
            }
        )

        regular_config = instance._user_config(FakeEvent("10001"))
        admin_config = instance._user_config(FakeEvent("90001", admin=True))

        self.assertEqual(regular_config["qb_username"], "personal-user")
        self.assertEqual(regular_config["qb_url"], "")
        self.assertEqual(regular_config["provider_cookie"], "")
        self.assertEqual(regular_config["provider_base_url"], "https://www.tjupt.org/")
        self.assertEqual(admin_config["qb_url"], "http://global:8080")
        self.assertEqual(admin_config["provider_cookie"], "global-cookie")

    def test_legacy_event_admin_role_is_supported(self):
        class LegacyEvent:
            role = "admin"

            def get_sender_id(self):
                return "90001"

        instance = make_plugin(
            {
                "access_blacklist": ["90001"],
                "access_admin_bypass": True,
            }
        )

        instance._ensure_user_access(LegacyEvent())


class BatchDownloadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.instance = make_plugin({})
        self.results = make_results(5)

    def test_multiple_indexes_are_selected_in_order(self):
        selected = self.instance._pick_results("1 3 5", self.results)

        self.assertEqual([torrent.index for torrent in selected], [1, 3, 5])

    def test_commas_and_duplicate_indexes_are_supported(self):
        selected = self.instance._pick_results("1,3，3 5", self.results)

        self.assertEqual([torrent.index for torrent in selected], [1, 3, 5])

    def test_any_out_of_range_index_rejects_the_whole_selection(self):
        with self.assertRaisesRegex(plugin_module.UserFacingError, "无效序号：4、6"):
            self.instance._pick_results("1 4 6", make_results(3))

    def test_non_numeric_index_is_rejected(self):
        with self.assertRaisesRegex(plugin_module.UserFacingError, "序号格式错误：abc"):
            self.instance._pick_results("1 abc 3", self.results)

    async def test_handler_reports_partial_client_failures(self):
        event = FakeEvent("10001")
        self.instance._search_cache["10001"] = (
            time.time(),
            600,
            make_results(3),
        )

        async def fake_add(torrent, _user_config):
            if torrent.index == 2:
                raise plugin_module.UserFacingError("客户端拒绝任务")

        self.instance._add_to_download_client = fake_add

        response = await anext(self.instance.download_torrent(event, "1 2 3"))

        self.assertIn("已添加到 qBittorrent：2 个任务", response)
        self.assertIn("添加失败：1 个任务", response)
        self.assertIn("2. 种子 2：客户端拒绝任务", response)


class QBittorrentWebAPITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.instance = make_plugin({})
        self.config = {"username": "tester", "password": "secret"}

    @staticmethod
    def make_client(status_code, body=""):
        class FakeClient:
            async def post(self, _url, data):
                return plugin_module.httpx.Response(status_code, text=body)

        return FakeClient()

    async def test_accepts_qbittorrent_52_no_content_response(self):
        client = self.make_client(204)

        await self.instance._login_qbittorrent(client, self.config)

    async def test_accepts_legacy_ok_response(self):
        client = self.make_client(200, "Ok.")

        await self.instance._login_qbittorrent(client, self.config)

    async def test_legacy_failed_response_reports_invalid_credentials(self):
        client = self.make_client(200, "Fails.")

        with self.assertRaisesRegex(plugin_module.UserFacingError, "用户名和密码"):
            await self.instance._login_qbittorrent(client, self.config)

    async def test_unauthorized_response_reports_invalid_credentials(self):
        client = self.make_client(401)

        with self.assertRaisesRegex(plugin_module.UserFacingError, "用户名和密码"):
            await self.instance._login_qbittorrent(client, self.config)

    async def test_forbidden_response_mentions_temporary_ban(self):
        client = self.make_client(403)

        with self.assertRaisesRegex(plugin_module.UserFacingError, "临时封禁"):
            await self.instance._login_qbittorrent(client, self.config)

    async def test_unrecognized_success_response_is_not_reported_as_bad_password(self):
        client = self.make_client(200, "Unexpected")

        with self.assertRaisesRegex(plugin_module.UserFacingError, "无法识别"):
            await self.instance._login_qbittorrent(client, self.config)

    def test_accepts_qbittorrent_52_structured_add_response(self):
        response = plugin_module.httpx.Response(
            200,
            json={
                "success_count": 1,
                "pending_count": 0,
                "failure_count": 0,
                "added_torrent_ids": ["hash"],
            },
        )

        self.instance._check_qbittorrent_add_response(response, "添加失败")

    def test_accepts_qbittorrent_52_pending_add_response(self):
        response = plugin_module.httpx.Response(
            202,
            json={"success_count": 0, "pending_count": 1, "failure_count": 0},
        )

        self.instance._check_qbittorrent_add_response(response, "添加失败")

    def test_accepts_no_content_add_response(self):
        response = plugin_module.httpx.Response(204)

        self.instance._check_qbittorrent_add_response(response, "添加失败")

    def test_conflict_add_response_reports_rejected_task(self):
        response = plugin_module.httpx.Response(409)

        with self.assertRaisesRegex(plugin_module.UserFacingError, "可能已存在"):
            self.instance._check_qbittorrent_add_response(response, "添加失败")


if __name__ == "__main__":
    unittest.main()
