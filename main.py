import asyncio
import html
import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - handled at runtime for friendly errors
    BeautifulSoup = None


PLUGIN_DIR = Path(__file__).resolve().parent
CARD_TEMPLATE = PLUGIN_DIR / "templates" / "torrent_results.html"
DEFAULT_RENDER_WIDTH = 900
RENDER_BASE_HEIGHT = 214
RENDER_ITEM_HEIGHT = 108
PLUGIN_VERSION = "1.2.0"


@dataclass(frozen=True)
class ConfigField:
    label: str
    value_type: str = "string"
    minimum: float | None = None
    maximum: float | None = None
    sensitive: bool = False


USER_CONFIG_FIELDS = {
    "provider_base_url": ConfigField("PT 站点地址"),
    "provider_search_path": ConfigField("PT 搜索路径"),
    "provider_cookie": ConfigField("PT Cookie", sensitive=True),
    "download_client": ConfigField("下载客户端"),
    "qb_url": ConfigField("qBittorrent WebUI 地址"),
    "qb_username": ConfigField("qBittorrent 用户名"),
    "qb_password": ConfigField("qBittorrent 密码", sensitive=True),
    "qb_save_path": ConfigField("qBittorrent 保存路径"),
    "qb_category": ConfigField("qBittorrent 分类"),
    "qb_paused": ConfigField("qBittorrent 添加后暂停", "bool"),
    "ut_url": ConfigField("uTorrent WebUI 地址"),
    "ut_username": ConfigField("uTorrent 用户名"),
    "ut_password": ConfigField("uTorrent 密码", sensitive=True),
    "direct_torrent_max_size_mb": ConfigField("直链种子大小上限（MB）", "int", 1, 500),
    "max_results": ConfigField("最大搜索结果数", "int", 1, 30),
    "cache_ttl_seconds": ConfigField("搜索缓存时间（秒）", "int", 60, 3600),
    "render_width": ConfigField("结果卡片宽度", "int", 640, 1400),
    "request_timeout": ConfigField("请求超时（秒）", "float", 5, 120),
}

BUILTIN_USER_CONFIG = {
    "provider_base_url": "https://www.tjupt.org/",
    "provider_search_path": "torrents.php?search={keyword}&incldead=0",
    "provider_cookie": "",
    "download_client": "qbittorrent",
    "qb_url": "",
    "qb_username": "",
    "qb_password": "",
    "qb_save_path": "",
    "qb_category": "",
    "qb_paused": False,
    "ut_url": "",
    "ut_username": "",
    "ut_password": "",
    "direct_torrent_max_size_mb": 50,
    "max_results": 10,
    "cache_ttl_seconds": 600,
    "render_width": 900,
    "request_timeout": 20.0,
}

CONFIG_KEY_ALIASES = {
    "站点": "provider_base_url",
    "pt站点": "provider_base_url",
    "搜索路径": "provider_search_path",
    "cookie": "provider_cookie",
    "客户端": "download_client",
    "qb地址": "qb_url",
    "qb用户名": "qb_username",
    "qb账号": "qb_username",
    "qb密码": "qb_password",
    "qb保存路径": "qb_save_path",
    "qb分类": "qb_category",
    "qb暂停": "qb_paused",
    "ut地址": "ut_url",
    "ut用户名": "ut_username",
    "ut账号": "ut_username",
    "ut密码": "ut_password",
    "种子大小上限": "direct_torrent_max_size_mb",
    "结果数": "max_results",
    "缓存时间": "cache_ttl_seconds",
    "卡片宽度": "render_width",
    "超时": "request_timeout",
}


@dataclass
class TorrentResult:
    index: int
    title: str
    subtitle: str
    size: str
    seeders: str
    leechers: str
    completed: str
    detail_url: str
    download_url: str


@dataclass
class TorrentFile:
    filename: str
    content: bytes
    content_type: str


@dataclass
class DirectDownload:
    uri: str
    title: str


@register(
    "astrbot_plugin_qbittorrent_manager",
    "Codex",
    "按 QQ 用户配置 PT 站并推送到 qBittorrent 或 uTorrent 下载。",
    PLUGIN_VERSION,
)
class QBittorrentManagerPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._search_cache: dict[str, tuple[float, int, list[TorrentResult]]] = {}
        self._config_lock = asyncio.Lock()

    @filter.command("种子")
    async def search_torrents(self, event: AstrMessageEvent, keyword: str = ""):
        try:
            self._ensure_user_access(event)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return

        keyword = keyword.strip()
        if not keyword:
            yield event.plain_result(self._help_text())
            return

        if BeautifulSoup is None:
            yield event.plain_result(
                "缺少依赖 beautifulsoup4，请先安装插件依赖后重试。"
            )
            return

        try:
            user_config = self._user_config(event)
            results = await self._search(keyword, user_config)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return
        except Exception as error:  # noqa: BLE001
            logger.exception("搜索种子失败")
            yield event.plain_result(f"搜索失败：{error}")
            return

        if not results:
            yield event.plain_result(f"没有找到与「{keyword}」相关的种子。")
            return

        self._remember_results(event, results, user_config)
        image_url = await self._render_results(keyword, results, user_config)
        if image_url:
            yield event.image_result(image_url)
            return

        yield event.plain_result(self._format_plain_results(keyword, results))

    @filter.command("种子下载")
    async def download_torrent(self, event: AstrMessageEvent, selection: GreedyStr):
        try:
            self._ensure_user_access(event)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return

        selection = str(selection).strip()
        if not selection:
            yield event.plain_result("请发送 /种子下载 序号，例如：/种子下载 1 3 5")
            return

        try:
            user_config = self._user_config(event)
            results = self._get_cached_results(event)
            if not results:
                yield event.plain_result(
                    "没有可下载的搜索结果，请先使用 /种子 关键词 搜索。"
                )
                return

            torrents = self._pick_results(selection, results)
            client_name = self._download_client_name(user_config)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return
        except Exception as error:  # noqa: BLE001
            logger.exception("添加下载任务失败")
            yield event.plain_result(f"添加下载失败：{error}")
            return

        succeeded: list[TorrentResult] = []
        failed: list[tuple[TorrentResult, str]] = []
        for torrent in torrents:
            try:
                await self._add_to_download_client(torrent, user_config)
                succeeded.append(torrent)
            except UserFacingError as error:
                failed.append((torrent, str(error)))
            except Exception as error:  # noqa: BLE001
                logger.exception("添加下载任务失败：%s", torrent.title)
                failed.append((torrent, f"添加下载失败：{error}"))

        yield event.plain_result(
            self._format_batch_download_result(client_name, succeeded, failed)
        )

    @filter.command("直接下载")
    async def direct_download(self, event: AstrMessageEvent, uri: str = ""):
        try:
            self._ensure_user_access(event)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return

        uri = uri.strip()
        if not uri:
            yield event.plain_result(
                "请发送 /直接下载 磁链或种子链接，例如：/直接下载 magnet:?xt=..."
            )
            return
        if not self._is_supported_direct_uri(uri):
            yield event.plain_result(
                "链接格式不支持，请提供 magnet、http 或 https 链接。"
            )
            return

        try:
            user_config = self._user_config(event)
            client_name = self._download_client_name(user_config)
            direct_download = DirectDownload(uri=uri, title=self._direct_title(uri))
            await self._add_direct_download(direct_download, user_config)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return
        except Exception as error:  # noqa: BLE001
            logger.exception("添加直接下载任务失败")
            yield event.plain_result(f"添加下载失败：{error}")
            return

        yield event.plain_result(f"已添加到 {client_name}：{direct_download.title}")

    @filter.command("磁链下载")
    async def magnet_download(self, event: AstrMessageEvent, uri: str = ""):
        async for result in self.direct_download(event, uri):
            yield result

    @filter.command("链接下载")
    async def link_download(self, event: AstrMessageEvent, uri: str = ""):
        async for result in self.direct_download(event, uri):
            yield result

    @filter.command("种子帮助")
    async def torrent_help(self, event: AstrMessageEvent):
        try:
            self._ensure_user_access(event)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return
        yield event.plain_result(self._help_text())

    @filter.command_group("种子配置")
    def torrent_config(self):
        pass

    @torrent_config.command("查看")
    async def show_user_config(self, event: AstrMessageEvent):
        try:
            self._ensure_user_access(event)
            user_id = self._user_id(event)
            user_config = self._user_config(event)
            overrides = self._user_overrides(user_id)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return

        yield event.plain_result(
            self._format_user_config(
                user_id,
                user_config,
                overrides,
                self._can_use_global_config(event),
            )
        )

    @torrent_config.command("设置")
    async def set_user_config(
        self,
        event: AstrMessageEvent,
        key: str,
        value: GreedyStr,
    ):
        try:
            self._ensure_user_access(event)
            if not self._user_config_commands_enabled():
                raise UserFacingError("管理员已关闭用户自助配置。")
            user_id = self._user_id(event)
            config_key = self._resolve_config_key(key)
            config_value = self._parse_user_config_value(config_key, str(value))
            await self._set_user_override(user_id, config_key, config_value)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return

        field = USER_CONFIG_FIELDS[config_key]
        message = f"已保存个人配置：{field.label}。"
        if field.sensitive and not event.is_private_chat():
            message += "\n你在群聊中发送了敏感信息，建议立即撤回并改用私聊配置。"
        yield event.plain_result(message)

    @torrent_config.command("删除")
    async def delete_user_config(
        self,
        event: AstrMessageEvent,
        key: str,
    ):
        try:
            self._ensure_user_access(event)
            if not self._user_config_commands_enabled():
                raise UserFacingError("管理员已关闭用户自助配置。")
            user_id = self._user_id(event)
            config_key = self._resolve_config_key(key)
            removed = await self._delete_user_override(user_id, config_key)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return

        if not removed:
            yield event.plain_result("该配置项没有个人设置。")
            return
        yield event.plain_result(
            f"已删除个人配置：{USER_CONFIG_FIELDS[config_key].label}，恢复全局默认值。"
        )

    @torrent_config.command("重置")
    async def reset_user_config(self, event: AstrMessageEvent):
        try:
            self._ensure_user_access(event)
            if not self._user_config_commands_enabled():
                raise UserFacingError("管理员已关闭用户自助配置。")
            user_id = self._user_id(event)
            removed = await self._reset_user_overrides(user_id)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return

        if not removed:
            yield event.plain_result("当前没有个人配置。")
            return
        yield event.plain_result("已清除全部个人配置，恢复使用全局默认值。")

    @torrent_config.command("帮助")
    async def user_config_help(self, event: AstrMessageEvent):
        try:
            self._ensure_user_access(event)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return
        yield event.plain_result(self._user_config_help_text())

    async def _search(
        self, keyword: str, user_config: dict[str, Any]
    ) -> list[TorrentResult]:
        provider = self._provider_config(user_config)
        base_url = provider["base_url"].rstrip("/") + "/"
        search_path = provider["search_path"]
        limit = self._int_config("max_results", 10, 1, 30, user_config)
        search_url = urljoin(base_url, search_path.format(keyword=quote(keyword)))

        async with self._http_client(provider, user_config) as client:
            response = await client.get(search_url)
            self._raise_for_response(response, "搜索接口请求失败")

        return self._parse_nexusphp_results(response.text, base_url, limit)

    def _parse_nexusphp_results(
        self,
        html_text: str,
        base_url: str,
        limit: int,
    ) -> list[TorrentResult]:
        soup = BeautifulSoup(html_text, "html.parser")
        results: list[TorrentResult] = []
        seen: set[str] = set()

        for row in soup.find_all("tr"):
            direct_cells = row.find_all("td", recursive=False)
            if len(direct_cells) < 6:
                continue

            download_link = row.find(
                "a",
                href=re.compile(r"(download|dl)\.php\?.*id=", re.IGNORECASE),
            )
            if not download_link or not download_link.get("href"):
                continue

            download_url = urljoin(base_url, download_link["href"])
            if download_url in seen:
                continue
            seen.add(download_url)

            detail_link = row.find(
                "a", href=re.compile(r"(details|detail)\.php\?.*id=", re.I)
            )
            title = self._extract_title(detail_link, row)
            if not title:
                continue

            cells = [cell.get_text(" ", strip=True) for cell in direct_cells]
            row_text = " ".join(cells)
            numbers = re.findall(r"\b\d+\b", row_text)
            size = self._extract_size(row_text)
            stats = self._extract_stats(row, numbers)
            subtitle = self._extract_subtitle(cells, title)

            results.append(
                TorrentResult(
                    index=len(results) + 1,
                    title=title,
                    subtitle=subtitle,
                    size=size,
                    seeders=stats[0],
                    leechers=stats[1],
                    completed=stats[2],
                    detail_url=urljoin(base_url, detail_link["href"])
                    if detail_link
                    else "",
                    download_url=download_url,
                )
            )
            if len(results) >= limit:
                break

        return results

    async def _add_to_download_client(
        self, torrent: TorrentResult, user_config: dict[str, Any]
    ):
        torrent_file = await self._download_torrent_file(torrent, user_config)
        client_type = self._download_client_type(user_config)
        if client_type == "utorrent":
            await self._add_to_utorrent(torrent_file, user_config)
            return
        await self._add_to_qbittorrent(torrent_file, user_config)

    async def _add_direct_download(
        self, direct_download: DirectDownload, user_config: dict[str, Any]
    ):
        if direct_download.uri.lower().startswith("magnet:"):
            await self._add_uri_to_download_client(direct_download.uri, user_config)
            return

        torrent_file = await self._download_torrent_from_url(
            direct_download.uri, user_config
        )
        client_type = self._download_client_type(user_config)
        if client_type == "utorrent":
            await self._add_to_utorrent(torrent_file, user_config)
            return
        await self._add_to_qbittorrent(torrent_file, user_config)

    async def _add_uri_to_download_client(self, uri: str, user_config: dict[str, Any]):
        client_type = self._download_client_type(user_config)
        if client_type == "utorrent":
            await self._add_uri_to_utorrent(uri, user_config)
            return
        await self._add_uri_to_qbittorrent(uri, user_config)

    async def _download_torrent_file(
        self, torrent: TorrentResult, user_config: dict[str, Any]
    ) -> TorrentFile:
        provider = self._provider_config(user_config)
        async with self._http_client(provider, user_config) as pt_client:
            torrent_response = await pt_client.get(torrent.download_url)
            self._raise_for_response(torrent_response, "下载种子文件失败")

        return TorrentFile(
            filename=self._safe_filename(torrent.title),
            content=torrent_response.content,
            content_type=mimetypes.types_map.get(
                ".torrent", "application/x-bittorrent"
            ),
        )

    async def _download_torrent_from_url(
        self, uri: str, user_config: dict[str, Any]
    ) -> TorrentFile:
        provider = self._provider_config(user_config)
        include_cookie = self._same_origin(uri, provider["base_url"])
        async with self._http_client(
            provider, user_config, include_cookie=include_cookie
        ) as client:
            response = await client.get(uri)
            self._raise_for_response(response, "下载种子链接失败")

        max_size = (
            self._int_config("direct_torrent_max_size_mb", 50, 1, 500, user_config)
            * 1024
            * 1024
        )
        if len(response.content) > max_size:
            raise UserFacingError(
                "种子链接响应过大，已拒绝上传。请确认链接会直接下载 .torrent 文件。"
            )

        if not self._looks_like_torrent_response(uri, response):
            raise UserFacingError(
                "该链接不像 .torrent 文件。如果这是磁链请以 magnet:? 开头；"
                "如果是普通下载链接，请确认浏览器打开会直接下载种子文件。"
            )

        filename = self._filename_from_response(uri, response)
        return TorrentFile(
            filename=filename,
            content=response.content,
            content_type=response.headers.get(
                "content-type", "application/x-bittorrent"
            ).split(";")[0],
        )

    async def _add_to_qbittorrent(
        self, torrent_file: TorrentFile, user_config: dict[str, Any]
    ):
        qb_config = self._qbittorrent_config(user_config)
        save_path = (user_config.get("qb_save_path") or "").strip()
        category = (user_config.get("qb_category") or "").strip()
        paused = bool(user_config.get("qb_paused", False))

        async with httpx.AsyncClient(
            base_url=qb_config["url"],
            timeout=self._float_config(
                "request_timeout", 20.0, 5.0, 120.0, user_config
            ),
        ) as qb_client:
            await self._login_qbittorrent(qb_client, qb_config)

            data: dict[str, Any] = {"paused": "true" if paused else "false"}
            if save_path:
                data["savepath"] = save_path
            if category:
                data["category"] = category

            files = {
                "torrents": (
                    torrent_file.filename,
                    torrent_file.content,
                    torrent_file.content_type,
                )
            }
            add_response = await qb_client.post(
                "/api/v2/torrents/add", data=data, files=files
            )
            self._raise_for_response(add_response, "qBittorrent 添加任务失败")
            if add_response.text.strip().lower() not in {"ok.", "ok"}:
                raise UserFacingError(
                    f"qBittorrent 返回异常：{add_response.text[:120]}"
                )

    async def _add_uri_to_qbittorrent(self, uri: str, user_config: dict[str, Any]):
        qb_config = self._qbittorrent_config(user_config)
        save_path = (user_config.get("qb_save_path") or "").strip()
        category = (user_config.get("qb_category") or "").strip()
        paused = bool(user_config.get("qb_paused", False))

        async with httpx.AsyncClient(
            base_url=qb_config["url"],
            timeout=self._float_config(
                "request_timeout", 20.0, 5.0, 120.0, user_config
            ),
        ) as qb_client:
            await self._login_qbittorrent(qb_client, qb_config)

            data: dict[str, Any] = {
                "urls": uri,
                "paused": "true" if paused else "false",
            }
            if save_path:
                data["savepath"] = save_path
            if category:
                data["category"] = category

            add_response = await qb_client.post("/api/v2/torrents/add", data=data)
            self._raise_for_response(add_response, "qBittorrent 添加链接失败")
            if add_response.text.strip().lower() not in {"ok.", "ok"}:
                raise UserFacingError(
                    f"qBittorrent 返回异常：{add_response.text[:120]}"
                )

    async def _add_to_utorrent(
        self, torrent_file: TorrentFile, user_config: dict[str, Any]
    ):
        ut_config = self._utorrent_config(user_config)
        async with httpx.AsyncClient(
            base_url=ut_config["url"],
            auth=self._utorrent_auth(ut_config),
            timeout=self._float_config(
                "request_timeout", 20.0, 5.0, 120.0, user_config
            ),
            follow_redirects=True,
        ) as ut_client:
            token = await self._get_utorrent_token(ut_client)

            files = {
                "torrent_file": (
                    torrent_file.filename,
                    torrent_file.content,
                    torrent_file.content_type,
                )
            }
            add_response = await ut_client.post(
                "/gui/",
                params={"action": "add-file", "token": token},
                files=files,
            )
            self._raise_for_response(add_response, "uTorrent 添加任务失败")
            body = add_response.text.strip().lower()
            if "invalid request" in body or "invalid token" in body:
                raise UserFacingError(f"uTorrent 返回异常：{add_response.text[:120]}")

    async def _add_uri_to_utorrent(self, uri: str, user_config: dict[str, Any]):
        ut_config = self._utorrent_config(user_config)
        async with httpx.AsyncClient(
            base_url=ut_config["url"],
            auth=self._utorrent_auth(ut_config),
            timeout=self._float_config(
                "request_timeout", 20.0, 5.0, 120.0, user_config
            ),
            follow_redirects=True,
        ) as ut_client:
            token = await self._get_utorrent_token(ut_client)
            add_response = await ut_client.get(
                "/gui/",
                params={"action": "add-url", "token": token, "s": uri},
            )
            self._raise_for_response(add_response, "uTorrent 添加链接失败")
            body = add_response.text.strip().lower()
            if "invalid request" in body or "invalid token" in body:
                raise UserFacingError(f"uTorrent 返回异常：{add_response.text[:120]}")

    async def _login_qbittorrent(
        self, qb_client: httpx.AsyncClient, qb_config: dict[str, str]
    ):
        if not (qb_config["username"] or qb_config["password"]):
            return

        login_response = await qb_client.post(
            "/api/v2/auth/login",
            data={
                "username": qb_config["username"],
                "password": qb_config["password"],
            },
        )
        self._raise_for_response(login_response, "qBittorrent 登录失败")
        if login_response.text.strip().lower() != "ok.":
            raise UserFacingError("qBittorrent 登录失败，请检查用户名和密码。")

    async def _get_utorrent_token(self, ut_client: httpx.AsyncClient) -> str:
        token_response = await ut_client.get("/gui/token.html")
        self._raise_for_response(token_response, "uTorrent 获取 Token 失败")
        token = self._extract_utorrent_token(token_response.text)
        if not token:
            raise UserFacingError(
                "uTorrent 获取 Token 失败，请检查 WebUI 地址、账号密码和 WebUI 是否启用。"
            )
        return token

    async def _render_results(
        self,
        keyword: str,
        results: list[TorrentResult],
        user_config: dict[str, Any],
    ) -> str | None:
        if not CARD_TEMPLATE.exists():
            return None

        render_width, render_height = self._render_dimensions(len(results), user_config)
        rows = "\n".join(
            f"""
            <article class="item">
              <div class="rank">{result.index}</div>
              <div class="content">
                <h2>{html.escape(result.title)}</h2>
                <p>{html.escape(result.subtitle or "暂无简介")}</p>
                <div class="meta">
                  <span>大小 {html.escape(result.size)}</span>
                  <span>做种 {html.escape(result.seeders)}</span>
                  <span>下载 {html.escape(result.leechers)}</span>
                  <span>完成 {html.escape(result.completed)}</span>
                </div>
              </div>
            </article>
            """
            for result in results
        )
        rendered_html = (
            CARD_TEMPLATE.read_text(encoding="utf-8")
            .replace(
                "{{ keyword }}",
                html.escape(keyword),
            )
            .replace("{{ results }}", rows)
            .replace("{{ render_width }}", str(render_width))
            .replace("{{ render_height }}", str(render_height))
            .replace(
                "{{ client_name }}",
                html.escape(self._download_client_name(user_config)),
            )
        )

        try:
            return await self.html_render(
                rendered_html,
                {},
                options={
                    "viewport_width": render_width,
                    "viewport_height": render_height,
                    "full_page": False,
                    "clip": {
                        "x": 0,
                        "y": 0,
                        "width": render_width,
                        "height": render_height,
                    },
                    "type": "png",
                    "scale": "css",
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("t2i 渲染失败，回退纯文本结果")
            return None

    def _provider_config(self, user_config: dict[str, Any]) -> dict[str, str]:
        base_url = (
            user_config.get("provider_base_url") or "https://www.tjupt.org/"
        ).strip()
        search_path = (
            user_config.get("provider_search_path")
            or "torrents.php?search={keyword}&incldead=0"
        ).strip()
        cookie = (user_config.get("provider_cookie") or "").strip()
        if not base_url:
            raise UserFacingError("未配置 PT 站点地址。")
        if "{keyword}" not in search_path:
            raise UserFacingError("搜索路径必须包含 {keyword} 占位符。")
        return {"base_url": base_url, "search_path": search_path, "cookie": cookie}

    def _qbittorrent_config(self, user_config: dict[str, Any]) -> dict[str, str]:
        qb_url = (user_config.get("qb_url") or "").strip().rstrip("/")
        if not qb_url:
            raise UserFacingError("未配置 qBittorrent WebUI 地址。")
        return {
            "url": qb_url,
            "username": (user_config.get("qb_username") or "").strip(),
            "password": (user_config.get("qb_password") or "").strip(),
        }

    def _utorrent_config(self, user_config: dict[str, Any]) -> dict[str, str]:
        ut_url = (user_config.get("ut_url") or "").strip().rstrip("/")
        if ut_url.endswith("/gui"):
            ut_url = ut_url.removesuffix("/gui")
        if not ut_url:
            raise UserFacingError("未配置 uTorrent WebUI 地址。")
        return {
            "url": ut_url,
            "username": (user_config.get("ut_username") or "").strip(),
            "password": (user_config.get("ut_password") or "").strip(),
        }

    def _download_client_type(self, user_config: dict[str, Any]) -> str:
        client_type = (user_config.get("download_client") or "qbittorrent").strip()
        client_type = client_type.lower().replace("-", "").replace("_", "")
        if client_type in {"utorrent", "μtorrent"}:
            return "utorrent"
        return "qbittorrent"

    def _download_client_name(self, user_config: dict[str, Any]) -> str:
        if self._download_client_type(user_config) == "utorrent":
            return "uTorrent"
        return "qBittorrent"

    @staticmethod
    def _utorrent_auth(ut_config: dict[str, str]) -> tuple[str, str] | None:
        if ut_config["username"] or ut_config["password"]:
            return ut_config["username"], ut_config["password"]
        return None

    def _http_client(
        self,
        provider: dict[str, str],
        user_config: dict[str, Any],
        *,
        include_cookie: bool = True,
    ) -> httpx.AsyncClient:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            )
        }
        if include_cookie and provider.get("cookie"):
            headers["Cookie"] = provider["cookie"]
        return httpx.AsyncClient(
            headers=headers,
            timeout=self._float_config(
                "request_timeout", 20.0, 5.0, 120.0, user_config
            ),
            follow_redirects=True,
        )

    def _render_dimensions(
        self, result_count: int, user_config: dict[str, Any]
    ) -> tuple[int, int]:
        width = self._int_config(
            "render_width", DEFAULT_RENDER_WIDTH, 640, 1400, user_config
        )
        height = RENDER_BASE_HEIGHT + max(result_count, 1) * RENDER_ITEM_HEIGHT
        height = min(max(height, 360), 2000)
        return width, height

    def _remember_results(
        self,
        event: AstrMessageEvent,
        results: list[TorrentResult],
        user_config: dict[str, Any],
    ):
        self._cleanup_cache()
        ttl = self._int_config("cache_ttl_seconds", 600, 60, 3600, user_config)
        self._search_cache[self._cache_key(event)] = (time.time(), ttl, results)

    def _get_cached_results(self, event: AstrMessageEvent) -> list[TorrentResult]:
        cached = self._search_cache.get(self._cache_key(event))
        if not cached:
            return []
        created_at, ttl, results = cached
        if time.time() - created_at > ttl:
            self._search_cache.pop(self._cache_key(event), None)
            return []
        return results

    def _cache_key(self, event: AstrMessageEvent) -> str:
        return self._user_id(event)

    @staticmethod
    def _same_origin(first_url: str, second_url: str) -> bool:
        first = urlparse(first_url)
        second = urlparse(second_url)

        def origin(parsed):
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return None
            default_port = 443 if parsed.scheme == "https" else 80
            return parsed.scheme, parsed.hostname.lower(), parsed.port or default_port

        return origin(first) is not None and origin(first) == origin(second)

    def _cleanup_cache(self):
        now = time.time()
        expired = [
            key
            for key, (created_at, ttl, _) in self._search_cache.items()
            if now - created_at > ttl
        ]
        for key in expired:
            self._search_cache.pop(key, None)

    def _user_id(self, event: AstrMessageEvent) -> str:
        user_id = str(event.get_sender_id() or "").strip()
        if not user_id:
            raise UserFacingError("无法识别你的 QQ 号，暂时不能使用该功能。")
        return user_id

    def _ensure_user_access(self, event: AstrMessageEvent):
        user_id = self._user_id(event)
        if self._admins_bypass_access_control() and self._event_is_admin(event):
            return

        blacklist = self._configured_user_ids("access_blacklist")
        if user_id in blacklist:
            raise UserFacingError("你已被加入插件黑名单，无法使用该功能。")

        whitelist = self._configured_user_ids("access_whitelist")
        if whitelist and user_id not in whitelist:
            raise UserFacingError("你不在插件白名单中，无法使用该功能。")

    def _configured_user_ids(self, key: str) -> set[str]:
        value = self.config.get(key, [])
        if isinstance(value, str):
            candidates = re.split(r"[\s,，;；]+", value)
        elif isinstance(value, list | tuple | set):
            candidates = value
        else:
            return set()
        return {str(item).strip() for item in candidates if str(item).strip()}

    def _admins_bypass_access_control(self) -> bool:
        return bool(self.config.get("access_admin_bypass", True))

    def _can_use_global_config(self, event: AstrMessageEvent) -> bool:
        if not bool(self.config.get("global_config_admin_only", False)):
            return True
        return self._event_is_admin(event)

    @staticmethod
    def _event_is_admin(event: AstrMessageEvent) -> bool:
        admin_value = getattr(event, "is_admin", None)
        if callable(admin_value):
            try:
                return bool(admin_value())
            except Exception:  # noqa: BLE001
                logger.exception("读取 AstrBot 管理员状态失败")
        elif admin_value is not None:
            return bool(admin_value)
        return getattr(event, "role", "member") == "admin"

    def _user_config(self, event: AstrMessageEvent) -> dict[str, Any]:
        user_id = self._user_id(event)
        merged: dict[str, Any] = dict(BUILTIN_USER_CONFIG)
        if self._can_use_global_config(event):
            merged.update(
                {
                    key: self.config.get(key)
                    for key in USER_CONFIG_FIELDS
                    if key in self.config
                }
            )
        merged.update(self._user_overrides(user_id))
        return merged

    def _user_overrides(self, user_id: str) -> dict[str, Any]:
        overrides: dict[str, Any] = {}
        for profile in self._user_profiles():
            if str(profile.get("qq") or "").strip() != user_id:
                continue
            for key in USER_CONFIG_FIELDS:
                if key in profile:
                    overrides[key] = profile[key]
        return overrides

    def _user_profiles(self) -> list[dict[str, Any]]:
        profiles = self.config.get("user_profiles", [])
        if not isinstance(profiles, list):
            return []
        return [profile for profile in profiles if isinstance(profile, dict)]

    def _user_config_commands_enabled(self) -> bool:
        return bool(self.config.get("enable_user_config_commands", True))

    def _resolve_config_key(self, key: str) -> str:
        normalized = key.strip().lower().replace("-", "_")
        config_key = CONFIG_KEY_ALIASES.get(normalized, normalized)
        if config_key not in USER_CONFIG_FIELDS:
            raise UserFacingError(
                f"未知配置项：{key}。发送 /种子配置 帮助 查看可用配置项。"
            )
        return config_key

    def _parse_user_config_value(self, key: str, value: str) -> Any:
        field = USER_CONFIG_FIELDS[key]
        value = value.strip()
        if not value:
            raise UserFacingError(
                "配置值不能为空；如需恢复默认值请使用 /种子配置 删除。"
            )

        if field.value_type == "bool":
            normalized = value.lower()
            if normalized in {"true", "yes", "1", "on", "是", "开启"}:
                parsed: Any = True
            elif normalized in {"false", "no", "0", "off", "否", "关闭"}:
                parsed = False
            else:
                raise UserFacingError("该配置项只接受 true/false、是/否或开启/关闭。")
        elif field.value_type == "int":
            try:
                parsed = int(value)
            except ValueError as error:
                raise UserFacingError("该配置项必须是整数。") from error
        elif field.value_type == "float":
            try:
                parsed = float(value)
            except ValueError as error:
                raise UserFacingError("该配置项必须是数字。") from error
        else:
            parsed = value

        if isinstance(parsed, int | float):
            if field.minimum is not None and parsed < field.minimum:
                raise UserFacingError(f"该配置项不能小于 {field.minimum:g}。")
            if field.maximum is not None and parsed > field.maximum:
                raise UserFacingError(f"该配置项不能大于 {field.maximum:g}。")

        if key == "download_client":
            normalized_client = value.lower().replace("-", "").replace("_", "")
            if normalized_client in {"utorrent", "μtorrent"}:
                return "utorrent"
            if normalized_client in {"qbittorrent", "qb"}:
                return "qbittorrent"
            raise UserFacingError("下载客户端只支持 qbittorrent 或 utorrent。")

        if key == "provider_search_path" and "{keyword}" not in value:
            raise UserFacingError("PT 搜索路径必须包含 {keyword} 占位符。")

        if key in {"provider_base_url", "qb_url", "ut_url"}:
            parsed_url = urlparse(value)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
                raise UserFacingError("地址必须是有效的 http 或 https URL。")

        return parsed

    async def _set_user_override(self, user_id: str, key: str, value: Any):
        async with self._config_lock:
            profiles = [dict(profile) for profile in self._user_profiles()]
            profile = self._merged_profile(user_id, profiles)
            profile[key] = value
            self._replace_user_profile(user_id, profiles, profile)
            await self._save_plugin_config(profiles)

    async def _delete_user_override(self, user_id: str, key: str) -> bool:
        async with self._config_lock:
            profiles = [dict(profile) for profile in self._user_profiles()]
            profile = self._merged_profile(user_id, profiles)
            if key not in profile:
                return False
            profile.pop(key)
            self._replace_user_profile(user_id, profiles, profile)
            await self._save_plugin_config(profiles)
            return True

    async def _reset_user_overrides(self, user_id: str) -> bool:
        async with self._config_lock:
            profiles = [dict(profile) for profile in self._user_profiles()]
            remaining = [
                profile
                for profile in profiles
                if str(profile.get("qq") or "").strip() != user_id
            ]
            if len(remaining) == len(profiles):
                return False
            await self._save_plugin_config(remaining)
            return True

    @staticmethod
    def _merged_profile(user_id: str, profiles: list[dict[str, Any]]) -> dict[str, Any]:
        merged: dict[str, Any] = {"__template_key": "user", "qq": user_id}
        for profile in profiles:
            if str(profile.get("qq") or "").strip() == user_id:
                merged.update(profile)
        merged["__template_key"] = "user"
        merged["qq"] = user_id
        return merged

    @staticmethod
    def _replace_user_profile(
        user_id: str,
        profiles: list[dict[str, Any]],
        replacement: dict[str, Any],
    ):
        profiles[:] = [
            profile
            for profile in profiles
            if str(profile.get("qq") or "").strip() != user_id
        ]
        if any(key in replacement for key in USER_CONFIG_FIELDS):
            profiles.append(replacement)

    async def _save_plugin_config(self, profiles: list[dict[str, Any]]):
        self.config["user_profiles"] = profiles
        save_config = getattr(self.config, "save_config", None)
        if callable(save_config):
            try:
                await asyncio.to_thread(save_config)
            except Exception as error:
                logger.exception("保存用户配置失败")
                raise UserFacingError("保存配置失败，请稍后重试。") from error

    def _format_user_config(
        self,
        user_id: str,
        user_config: dict[str, Any],
        overrides: dict[str, Any],
        global_config_allowed: bool,
    ) -> str:
        lines = [f"QQ {user_id} 的种子配置："]
        if global_config_allowed:
            lines.append("全局配置：允许继承")
        else:
            lines.append("全局配置：仅管理员可用，当前仅使用个人或内置默认配置")
        for key, field in USER_CONFIG_FIELDS.items():
            value = user_config.get(key)
            if field.sensitive:
                display = "已设置" if value else "未设置"
            elif isinstance(value, bool):
                display = "是" if value else "否"
            elif value in {None, ""}:
                display = "未设置"
            else:
                display = str(value)
            if key in overrides:
                source = "个人"
            elif global_config_allowed and key in self.config:
                source = "全局"
            else:
                source = "内置"
            lines.append(f"{field.label}：{display}（{source}）")
        lines.append("发送 /种子配置 帮助 查看设置方法。")
        return "\n".join(lines)

    @staticmethod
    def _pick_results(
        selection: str, results: list[TorrentResult]
    ) -> list[TorrentResult]:
        tokens = [token for token in re.split(r"[\s,，]+", selection.strip()) if token]
        if not tokens:
            raise UserFacingError("请至少提供一个种子序号。")

        invalid_tokens = [token for token in tokens if not token.isdigit()]
        if invalid_tokens:
            values = "、".join(invalid_tokens)
            raise UserFacingError(f"序号格式错误：{values}，请只输入数字。")

        indexes = list(dict.fromkeys(int(token) for token in tokens))
        invalid_indexes = [
            index for index in indexes if index < 1 or index > len(results)
        ]
        if invalid_indexes:
            values = "、".join(str(index) for index in invalid_indexes)
            raise UserFacingError(
                f"无效序号：{values}，当前只有 {len(results)} 条结果，"
                f"请在 1-{len(results)} 之间选择。"
            )

        return [results[index - 1] for index in indexes]

    @staticmethod
    def _format_batch_download_result(
        client_name: str,
        succeeded: list[TorrentResult],
        failed: list[tuple[TorrentResult, str]],
    ) -> str:
        lines: list[str] = []
        if succeeded:
            lines.append(f"已添加到 {client_name}：{len(succeeded)} 个任务")
            lines.extend(f"{torrent.index}. {torrent.title}" for torrent in succeeded)
        if failed:
            if lines:
                lines.append("")
            lines.append(f"添加失败：{len(failed)} 个任务")
            lines.extend(
                f"{torrent.index}. {torrent.title}：{reason}"
                for torrent, reason in failed
            )
        return "\n".join(lines) or "没有可添加的下载任务。"

    @staticmethod
    def _extract_title(detail_link: Any, row: Any) -> str:
        if detail_link:
            return (
                detail_link.get("title")
                or detail_link.get_text(" ", strip=True)
                or detail_link.find_parent().get_text(" ", strip=True)
            ).strip()
        return row.get_text(" ", strip=True)[:120].strip()

    @staticmethod
    def _extract_subtitle(cells: list[str], title: str) -> str:
        for cell in cells:
            subtitle = cell.replace(title, "", 1).strip()
            if len(subtitle) > 4:
                return subtitle[:120]
        return ""

    @staticmethod
    def _extract_size(text: str) -> str:
        match = re.search(
            r"(\d+(?:\.\d+)?\s*(?:TiB|GiB|MiB|KiB|TB|GB|MB|KB))", text, re.I
        )
        return match.group(1) if match else "未知"

    @staticmethod
    def _extract_stats(row: Any, numbers: list[str]) -> tuple[str, str, str]:
        classes = ("seeders", "leechers", "snatched", "completed")
        by_class: list[str] = []
        for class_name in classes:
            cell = row.find(class_=re.compile(class_name, re.I))
            if cell:
                by_class.append(cell.get_text(" ", strip=True) or "0")
        if len(by_class) >= 3:
            return by_class[0], by_class[1], by_class[2]
        if len(numbers) >= 3:
            return numbers[-3], numbers[-2], numbers[-1]
        return "?", "?", "?"

    @staticmethod
    def _raise_for_response(response: httpx.Response, prefix: str):
        if response.status_code in {401, 403}:
            raise UserFacingError(f"{prefix}：认证失败，请检查 Cookie 或账号权限。")
        if response.status_code >= 400:
            raise UserFacingError(f"{prefix}：HTTP {response.status_code}")

    @staticmethod
    def _safe_filename(title: str) -> str:
        filename = re.sub(r'[\\/:*?"<>|]+', "_", title).strip()[:120]
        return f"{filename or 'torrent'}.torrent"

    @staticmethod
    def _is_supported_direct_uri(uri: str) -> bool:
        lowered = uri.lower()
        if lowered.startswith("magnet:?"):
            return True
        parsed = urlparse(uri)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    @staticmethod
    def _direct_title(uri: str) -> str:
        if uri.lower().startswith("magnet:?"):
            display = uri[:80] + "..." if len(uri) > 80 else uri
            return f"磁链任务 {display}"
        parsed = urlparse(uri)
        filename = Path(unquote(parsed.path)).name
        return filename or parsed.netloc or "直接下载任务"

    @staticmethod
    def _looks_like_torrent_response(uri: str, response: httpx.Response) -> bool:
        content_type = response.headers.get("content-type", "").lower()
        content_disposition = response.headers.get("content-disposition", "").lower()
        lowered_uri = uri.lower()
        content = response.content.lstrip()

        return (
            "application/x-bittorrent" in content_type
            or "application/octet-stream" in content_type
            or ".torrent" in lowered_uri
            or ".torrent" in content_disposition
            or content.startswith(b"d")
        )

    @staticmethod
    def _filename_from_response(uri: str, response: httpx.Response) -> str:
        content_disposition = response.headers.get("content-disposition", "")
        filename_match = re.search(
            r'filename\*?=(?:UTF-8\'\')?["]?([^";]+)["]?',
            content_disposition,
            re.IGNORECASE,
        )
        if filename_match:
            filename = unquote(filename_match.group(1)).strip()
        else:
            filename = Path(unquote(urlparse(uri).path)).name

        filename = re.sub(r'[\\/:*?"<>|]+', "_", filename).strip()[:120]
        if not filename:
            filename = "direct-download.torrent"
        if not filename.lower().endswith(".torrent"):
            filename = f"{filename}.torrent"
        return filename

    @staticmethod
    def _extract_utorrent_token(response_text: str) -> str:
        match = re.search(
            r"<div[^>]+id=[\"']token[\"'][^>]*>([^<]+)</div>",
            response_text,
            re.IGNORECASE,
        )
        return match.group(1).strip() if match else ""

    def _format_plain_results(self, keyword: str, results: list[TorrentResult]) -> str:
        lines = [f"「{keyword}」搜索结果："]
        for result in results:
            lines.append(
                f"{result.index}. {result.title}\n"
                f"   大小：{result.size} 做种：{result.seeders} 下载：{result.leechers}"
            )
        lines.append("发送 /种子下载 序号... 批量添加到下载客户端。")
        return "\n".join(lines)

    @staticmethod
    def _help_text() -> str:
        return (
            "种子下载助手：\n"
            "1. /种子 关键词 - 搜索 PT 站种子\n"
            "2. /种子下载 序号... - 批量添加所选种子到下载客户端\n"
            "3. /直接下载 磁链或种子链接 - 直接添加下载任务\n"
            "4. /种子配置 帮助 - 配置自己的 PT 与下载客户端\n"
            "每位用户按 QQ 号使用独立配置。"
        )

    @staticmethod
    def _user_config_help_text() -> str:
        aliases = "、".join(CONFIG_KEY_ALIASES)
        return (
            "个人种子配置：\n"
            "/种子配置 查看\n"
            "/种子配置 设置 配置项 值\n"
            "/种子配置 删除 配置项\n"
            "/种子配置 重置\n"
            "示例：/种子配置 设置 客户端 qbittorrent\n"
            "示例：/种子配置 设置 qb地址 http://127.0.0.1:8080\n"
            "示例：/种子配置 设置 cookie access_token=你的值\n"
            f"常用配置项：{aliases}\n"
            "也可直接使用 AstrBot 面板中的英文配置键。\n"
            "Cookie 和密码建议仅在私聊中设置。"
        )

    def _int_config(
        self,
        key: str,
        default: int,
        minimum: int,
        maximum: int,
        user_config: dict[str, Any] | None = None,
    ) -> int:
        config = user_config if user_config is not None else self.config
        try:
            value = int(config.get(key, default))
        except (TypeError, ValueError):
            return default
        return min(max(value, minimum), maximum)

    def _float_config(
        self,
        key: str,
        default: float,
        minimum: float,
        maximum: float,
        user_config: dict[str, Any] | None = None,
    ) -> float:
        config = user_config if user_config is not None else self.config
        try:
            value = float(config.get(key, default))
        except (TypeError, ValueError):
            return default
        return min(max(value, minimum), maximum)

    async def terminate(self):
        await asyncio.sleep(0)


class UserFacingError(Exception):
    pass
