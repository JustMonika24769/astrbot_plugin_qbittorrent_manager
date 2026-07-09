import asyncio
import html
import mimetypes
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin

import httpx
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover - handled at runtime for friendly errors
    BeautifulSoup = None


PLUGIN_DIR = Path(__file__).resolve().parent
CARD_TEMPLATE = PLUGIN_DIR / "templates" / "torrent_results.html"
DEFAULT_RENDER_WIDTH = 900
RENDER_BASE_HEIGHT = 214
RENDER_ITEM_HEIGHT = 108


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


@register(
    "astrbot_plugin_qbittorrent_manager",
    "Codex",
    "在 PT 站搜索种子并推送到 qBittorrent 下载。",
    "0.1.0",
)
class QBittorrentManagerPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._search_cache: dict[str, tuple[float, list[TorrentResult]]] = {}

    @filter.command("种子")
    async def search_torrents(self, event: AstrMessageEvent, keyword: str = ""):
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
            results = await self._search(keyword)
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

        self._remember_results(event, results)
        image_url = await self._render_results(keyword, results)
        if image_url:
            yield event.image_result(image_url)
            return

        yield event.plain_result(self._format_plain_results(keyword, results))

    @filter.command("种子下载")
    async def download_torrent(self, event: AstrMessageEvent, selection: str = ""):
        selection = selection.strip()
        if not selection:
            yield event.plain_result("请发送 /种子下载 序号，例如：/种子下载 1")
            return

        results = self._get_cached_results(event)
        if not results:
            yield event.plain_result(
                "没有可下载的搜索结果，请先使用 /种子 关键词 搜索。"
            )
            return

        torrent = self._pick_result(selection, results)
        if torrent is None:
            yield event.plain_result(
                f"无效序号：{selection}，请在 1-{len(results)} 之间选择。"
            )
            return

        try:
            await self._add_to_qbittorrent(torrent)
        except UserFacingError as error:
            yield event.plain_result(str(error))
            return
        except Exception as error:  # noqa: BLE001
            logger.exception("添加 qBittorrent 下载失败")
            yield event.plain_result(f"添加下载失败：{error}")
            return

        yield event.plain_result(f"已添加到 qBittorrent：{torrent.title}")

    @filter.command("种子帮助")
    async def torrent_help(self, event: AstrMessageEvent):
        yield event.plain_result(self._help_text())

    async def _search(self, keyword: str) -> list[TorrentResult]:
        provider = self._provider_config()
        base_url = provider["base_url"].rstrip("/") + "/"
        search_path = provider["search_path"]
        limit = self._int_config("max_results", 10, 1, 30)
        search_url = urljoin(base_url, search_path.format(keyword=quote(keyword)))

        async with self._http_client(provider) as client:
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

            cells = [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
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

    async def _add_to_qbittorrent(self, torrent: TorrentResult):
        qb_config = self._qbittorrent_config()
        save_path = (self.config.get("qb_save_path") or "").strip()
        category = (self.config.get("qb_category") or "").strip()
        paused = bool(self.config.get("qb_paused", False))

        provider = self._provider_config()
        async with self._http_client(provider) as pt_client:
            torrent_response = await pt_client.get(torrent.download_url)
            self._raise_for_response(torrent_response, "下载种子文件失败")

        filename = self._safe_filename(torrent.title)
        async with httpx.AsyncClient(
            base_url=qb_config["url"],
            timeout=self._float_config("request_timeout", 20.0, 5.0, 120.0),
        ) as qb_client:
            if qb_config["username"] or qb_config["password"]:
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

            data: dict[str, Any] = {"paused": "true" if paused else "false"}
            if save_path:
                data["savepath"] = save_path
            if category:
                data["category"] = category

            files = {
                "torrents": (
                    filename,
                    torrent_response.content,
                    mimetypes.types_map.get(".torrent", "application/x-bittorrent"),
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

    async def _render_results(
        self, keyword: str, results: list[TorrentResult]
    ) -> str | None:
        if not CARD_TEMPLATE.exists():
            return None

        render_width, render_height = self._render_dimensions(len(results))
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

    def _provider_config(self) -> dict[str, str]:
        base_url = (
            self.config.get("provider_base_url") or "https://www.tjupt.org/"
        ).strip()
        search_path = (
            self.config.get("provider_search_path")
            or "torrents.php?search={keyword}&incldead=0"
        ).strip()
        cookie = (self.config.get("provider_cookie") or "").strip()
        if not base_url:
            raise UserFacingError("未配置 PT 站点地址。")
        if "{keyword}" not in search_path:
            raise UserFacingError("搜索路径必须包含 {keyword} 占位符。")
        return {"base_url": base_url, "search_path": search_path, "cookie": cookie}

    def _qbittorrent_config(self) -> dict[str, str]:
        qb_url = (self.config.get("qb_url") or "").strip().rstrip("/")
        if not qb_url:
            raise UserFacingError("未配置 qBittorrent WebUI 地址。")
        return {
            "url": qb_url,
            "username": (self.config.get("qb_username") or "").strip(),
            "password": (self.config.get("qb_password") or "").strip(),
        }

    def _http_client(self, provider: dict[str, str]) -> httpx.AsyncClient:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
            )
        }
        if provider.get("cookie"):
            headers["Cookie"] = provider["cookie"]
        return httpx.AsyncClient(
            headers=headers,
            timeout=self._float_config("request_timeout", 20.0, 5.0, 120.0),
            follow_redirects=True,
        )

    def _render_dimensions(self, result_count: int) -> tuple[int, int]:
        width = self._int_config("render_width", DEFAULT_RENDER_WIDTH, 640, 1400)
        height = RENDER_BASE_HEIGHT + max(result_count, 1) * RENDER_ITEM_HEIGHT
        height = min(max(height, 360), 2000)
        return width, height

    def _remember_results(self, event: AstrMessageEvent, results: list[TorrentResult]):
        self._cleanup_cache()
        self._search_cache[self._cache_key(event)] = (time.time(), results)

    def _get_cached_results(self, event: AstrMessageEvent) -> list[TorrentResult]:
        cached = self._search_cache.get(self._cache_key(event))
        if not cached:
            return []
        created_at, results = cached
        if time.time() - created_at > self._int_config(
            "cache_ttl_seconds", 600, 60, 3600
        ):
            return []
        return results

    def _cache_key(self, event: AstrMessageEvent) -> str:
        sender = event.get_sender_id() or "unknown"
        origin = event.unified_msg_origin or "unknown"
        return f"{origin}:{sender}"

    def _cleanup_cache(self):
        ttl = self._int_config("cache_ttl_seconds", 600, 60, 3600)
        now = time.time()
        expired = [
            key
            for key, (created_at, _) in self._search_cache.items()
            if now - created_at > ttl
        ]
        for key in expired:
            self._search_cache.pop(key, None)

    def _pick_result(
        self, selection: str, results: list[TorrentResult]
    ) -> TorrentResult | None:
        if not selection.isdigit():
            return None
        index = int(selection)
        if index < 1 or index > len(results):
            return None
        return results[index - 1]

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
            if title not in cell and len(cell) > 4:
                return cell[:120]
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

    def _format_plain_results(self, keyword: str, results: list[TorrentResult]) -> str:
        lines = [f"「{keyword}」搜索结果："]
        for result in results:
            lines.append(
                f"{result.index}. {result.title}\n"
                f"   大小：{result.size} 做种：{result.seeders} 下载：{result.leechers}"
            )
        lines.append("发送 /种子下载 序号 添加到 qBittorrent。")
        return "\n".join(lines)

    @staticmethod
    def _help_text() -> str:
        return (
            "种子下载助手：\n"
            "1. /种子 关键词 - 搜索 PT 站种子\n"
            "2. /种子下载 序号 - 将所选种子添加到 qBittorrent\n"
            "请先在插件配置中填写 PT Cookie 和 qBittorrent WebUI 信息。"
        )

    def _int_config(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default
        return min(max(value, minimum), maximum)

    def _float_config(
        self, key: str, default: float, minimum: float, maximum: float
    ) -> float:
        try:
            value = float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default
        return min(max(value, minimum), maximum)

    async def terminate(self):
        await asyncio.sleep(0)


class UserFacingError(Exception):
    pass
