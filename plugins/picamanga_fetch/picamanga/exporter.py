"""同步编排：全量 / 增量两种模式，把收藏夹合并进本地状态库。

排序与增量的正确性
------------------

收藏夹接口 ``/users/favourite?s=<sort>&page=<n>`` 的排序值由 ``sort`` 决定：

=========== ============================== ==============================
``sort``    含义                           能否用于增量
=========== ============================== ==============================
``dd``      按收藏日期**新到旧**           ✅ 唯一安全选项（默认）
``da``      按收藏日期旧到新               ❌ 新收藏在列表尾部
``ua``      默认（不保证新收藏在头部）      ❌ 同上
``ld``/``vd`` 按爱心 / 按指名热度          ❌ 和收藏时间无关
=========== ============================== ==============================

.. warning::
   哔咔**不返回收藏时间**，也没有任何形式的「加入书架编号」。列表里能拿到的
   只有漫画自己的 ``_id``（MongoDB ObjectId，前 4 字节是这本漫画的上架时间），
   它和「什么时候收藏的」没有关系——实测把「``_id`` 是否单调」当排序键，
   会把账号批量整理收藏夹留下的痕迹误判成排序规则变化。

   所以拷贝漫画那份的「数字编号水位线」在这里没有对应物。增量判定边界用的是
   **连续命中本地已有条目**（``consecutive_known_stop``，默认 5 条）：

   * 只命中 1 条就停手是不安全的——取消收藏后重新收藏会把那条老条目刷回列表头部，
     它后面紧跟的可能全是新收藏；
   * 要求连续命中 N 条才停，一次「跳变」只会造成一次命中，计数随即归零。

顺序前提被打破时怎么办
----------------------

增量扫描结束后再校验一条不依赖编号的不变量：远端总数（上次记在状态库 meta 里的
``server_total``）**变多了**，但这次从头部一条新收藏都没找到 → 说明最新的收藏
不在头部，也就是 ``dd`` 不再等价于「新收藏在前」。此时自动降级为全量扫描
（报告里 ``sort_verified=false``、``fell_back_to_full=true``），
宁可多花几个请求也不静默漏数据。

两种模式的区别
--------------

``full``（全量）
    遍历远端所有页；本地有、远端没有的条目标记为 ``removed=true``（已取消收藏），
    只打标记不删除，方便回溯。**只有完整翻完且没有任何一页抓取失败时才敢打标记**
    ——「这次没抓到」和「用户取消了收藏」是两件事。

``incremental``（增量）
    从第 1 页开始翻，连续命中本地已有条目就立即停止，日常刷新通常 1~2 次请求。
    已存在条目的字段（爱心数、话数、是否完结、封面地址）会在这条路径上顺带刷新。
    增量在半路停手时**不会**标记取消收藏——它只翻了头部；只有冷启动那种
    「本地一条都没有、于是把整份列表翻完」的情况才会打标记。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Set

from .client import PicamangaClient
from .config import FAVOURITE_PATH, MODES, NEWEST_FIRST_SORT, PAGE_SIZE, PicamangaConfig
from .errors import ApiError, ConfigError
from .models import (
    STOP_COMPLETE,
    STOP_KNOWN_BOUNDARY,
    STOP_MAX_PAGES,
    VERIFY_BASELINE,
    VERIFY_CHANGED_SCAN,
    VERIFY_FULL_SCAN,
    ComicItem,
    SyncReport,
    now_iso,
)
from .store import JsonStore


class FavoritesExporter:
    """把「拉取收藏夹 → 合并进本地状态库」串起来的门面类。

    用法一（自己给配置）::

        exporter = FavoritesExporter(config=PicamangaConfig(token="...", mode="full"))
        report = exporter.sync()

    用法二（复用已有 client）::

        client = PicamangaClient(token="...")
        exporter = FavoritesExporter(client)
        report = exporter.sync(mode="incremental")

    :param client: 可选的 :class:`~picamanga.client.PicamangaClient`，省略则按 config 新建。
        只要对象暴露 ``config`` / ``token`` / ``close()`` / ``fetch_favourite_page()``
        就能传进来（调用方常用一个包一层来上报进度）。
    :param config: 可选的 :class:`~picamanga.config.PicamangaConfig`。
    :param overrides: 直接覆盖配置字段。
    """

    def __init__(
        self,
        client: Optional[PicamangaClient] = None,
        config: Optional[PicamangaConfig] = None,
        **overrides: Any,
    ) -> None:
        if config is None:
            config = client.config if client is not None else PicamangaConfig()
        if overrides:
            config = config.merge(**overrides)
        self.config = config.validate()
        self.client = client or PicamangaClient(self.config)
        self.client.config = self.config  # 让超时/重试等行为和这次同步一致
        if self.client.token:
            self.config = self.config.merge(token=self.client.token)

    # ------------------------------------------------------------------
    # 只读入口
    # ------------------------------------------------------------------

    def load(self) -> List[ComicItem]:
        """只读本地状态库，不联网。"""
        return list(JsonStore(self.config.store_path).load())

    # ------------------------------------------------------------------
    # 同步
    # ------------------------------------------------------------------

    def sync(self, mode: Optional[str] = None) -> SyncReport:
        """执行一次同步并写入状态库 JSON。

        增量模式会校验「新收藏在头部」这个前提；前提不成立时自动降级成全量扫描
        （报告里 ``sort_verified=False``、``fell_back_to_full=True``），
        宁可多跑几个请求也不静默漏数据。

        :param mode: ``"full"`` 或 ``"incremental"``，省略用配置里的。
        :return: :class:`~picamanga.models.SyncReport`
        """
        cfg = self.config
        requested_mode = (mode or cfg.mode or "incremental").lower()
        if requested_mode not in MODES:
            raise ConfigError("mode 只能是 %s" % " / ".join(MODES))
        if requested_mode == "incremental" and cfg.sort != NEWEST_FIRST_SORT:
            raise ConfigError(
                "增量模式必须使用 sort=%r（按收藏日期新到旧），当前是 %r"
                % (NEWEST_FIRST_SORT, cfg.sort)
            )

        log = (lambda *a: None) if cfg.quiet else print
        started_at = now_iso()
        t0 = time.time()

        self.client.ensure_authenticated()
        log("· 认证就绪：%s | 单页固定 %d 条" % (cfg.base_url, PAGE_SIZE))

        store = JsonStore(cfg.store_path).load()
        known_ids: Set[str] = store.ids()
        prev_total = store.server_total()
        log("· 本地已有 %d 条 | 模式：%s | 排序：%s%s"
            % (len(known_ids), requested_mode, cfg.sort,
               "" if prev_total is None else " | 上次远端共 %d 条" % prev_total))

        # 先按请求的模式跑；发现顺序前提不成立时改用 full 重跑一遍
        mode_run = requested_mode
        result = self._scan(mode=mode_run, store=store, known_ids=known_ids, log=log)

        fell_back = False
        suspect = (
            self._sort_suspect(prev_total, result)
            if mode_run == "incremental" else None
        )
        if suspect and cfg.enforce_sort:
            log("· ⚠ %s，自动改跑全量扫描以免漏数据" % suspect)
            store = JsonStore(cfg.store_path).load()
            result = self._scan(mode="full", store=store, known_ids=store.ids(), log=log)
            mode_run = "full"
            fell_back = True

        # 「这次没抓到」不等于「用户取消了收藏」：只有完整、无失败地翻完才打标记
        complete = (
            not result["truncated"]
            and not result["reached_known"]
            and not result["errors"]
            and (bool(result["seen"]) or result["total_remote"] == 0)
        )
        removed = store.mark_removed(result["seen"], stamp=started_at) if complete else 0

        # 本地条目的排列 = 离列表头部的距离（新收藏在前）
        store.reorder(result["order"])

        finished_at = now_iso()
        verification = (
            VERIFY_BASELINE if not known_ids
            else VERIFY_FULL_SCAN if mode_run == "full" and complete
            else VERIFY_CHANGED_SCAN
        )
        store.meta.update(
            {
                "source": "%s%s" % (cfg.base_url, FAVOURITE_PATH),
                "host": cfg.base_url,
                "sort": cfg.sort,
                "page_size": PAGE_SIZE,
                "consecutive_known_stop": cfg.consecutive_known_stop,
                "server_total": result["total_remote"] or store.server_total() or 0,
                "updated_at": finished_at,
                "last_%s_sync" % mode_run: finished_at,
                "schema_version": JsonStore.SCHEMA_VERSION,
                "created_at": store.meta.get("created_at") or finished_at,
                "last_scan": {
                    "mode": mode_run,
                    "verification": verification,
                    "stop_reason": result["stop_reason"],
                    "pages": result["pages"],
                    "complete": complete,
                    "at": finished_at,
                },
            }
        )
        store.save(include_raw=cfg.include_raw)

        return SyncReport(
            mode=mode_run,
            host=cfg.base_url,
            sort=cfg.sort,
            started_at=started_at,
            finished_at=finished_at,
            duration_s=time.time() - t0,
            status="partial" if result["errors"] else "ok",
            total_remote=result["total_remote"],
            total_local=len(store),
            fetched=result["fetched"],
            added=result["added"],
            updated=result["updated"],
            unchanged=result["unchanged"],
            restored=result["restored"],
            removed=removed,
            pages=result["pages"],
            stop_reason=result["stop_reason"],
            verification=verification,
            consecutive_known_stop=cfg.consecutive_known_stop,
            sort_verified=suspect is None,
            fell_back_to_full=fell_back,
            truncated=result["truncated"],
            store_path=cfg.store_path,
            errors=list(result["errors"]),
        )

    # ------------------------------------------------------------------
    # 一次扫描（full / incremental 共用）
    # ------------------------------------------------------------------

    def _scan(
        self,
        *,
        mode: str,
        store: JsonStore,
        known_ids: Set[str],
        log,
    ) -> Dict[str, Any]:
        """从第 1 页开始翻页扫描远端，把结果合并进 ``store``。

        返回一个 dict：计数、页数、停止原因、扫描顺序、以及是否被截断/命中边界。
        注意这里**不写盘**，由 :meth:`sync` 决定是接受还是降级重跑。
        """
        cfg = self.config
        counts = {"added": 0, "updated": 0, "unchanged": 0, "restored": 0}
        seen: Set[str] = set()
        order: List[str] = []
        errors: List[str] = []
        pages = 0
        fetched = 0
        total_remote = 0
        stop_reason = STOP_COMPLETE
        reached_known = False
        truncated = False
        consecutive_known = 0

        page = 1
        total_pages = 1
        while True:
            if cfg.max_pages is not None and page > int(cfg.max_pages):
                stop_reason, truncated = STOP_MAX_PAGES, True
                log("· 达到单次页数上限（%s 页），本次先到这里" % cfg.max_pages)
                break

            try:
                docs, total, reported = self.client.fetch_favourite_page(
                    page, sort=cfg.sort
                )
            except ApiError as exc:
                # 失败的一页绝不能当成「列表到此结束」，否则后面的都会被误判成取消收藏
                errors.append("第 %d 页抓取失败：%s" % (page, exc))
                log("· ⚠ 第 %d 页抓取失败，停止本次扫描：%s" % (page, exc))
                stop_reason = "fetch-error"
                break

            pages += 1
            total_remote = max(total_remote, int(total or 0))
            if reported > 0:
                total_pages = int(reported)

            fresh_on_page = 0
            processed_on_page = 0
            for doc in docs:
                item = ComicItem.from_api(doc)
                if not item.comic_id or item.comic_id in seen:
                    continue
                seen.add(item.comic_id)
                order.append(item.comic_id)
                fetched += 1
                processed_on_page += 1
                was_known = item.comic_id in known_ids
                state = store.upsert(item)
                counts[state] += 1
                # 全新收藏、以及「之前标过取消收藏、这次又出现了」都算头部的新变化
                if not was_known or state == "restored":
                    fresh_on_page += 1

            # 连续命中判定：整页都是本地已有条目才累计，出现新收藏就归零
            if fresh_on_page:
                consecutive_known = 0
            else:
                consecutive_known += processed_on_page

            log("· 第 %d/%s 页：%d 条（新收藏 %d 条，连续已同步 %d 条）"
                % (page, total_pages or "?", processed_on_page, fresh_on_page,
                   consecutive_known))

            if not docs:
                break
            if mode == "incremental" and consecutive_known >= cfg.consecutive_known_stop:
                stop_reason, reached_known = STOP_KNOWN_BOUNDARY, True
                log("· 增量：已连续 %d 条是本地已有的收藏，后面不必再翻"
                    % consecutive_known)
                break
            if total_pages and page >= total_pages:
                break
            if total_remote and len(order) >= total_remote:
                break

            page += 1
            if cfg.request_delay:
                time.sleep(cfg.request_delay)

        return {
            **counts,
            "seen": seen,
            "order": order,
            "pages": pages,
            "fetched": fetched,
            "total_remote": total_remote,
            "stop_reason": stop_reason,
            "reached_known": reached_known,
            "truncated": truncated,
            "errors": errors,
        }

    # ------------------------------------------------------------------
    # 顺序前提
    # ------------------------------------------------------------------

    @staticmethod
    def _sort_suspect(prev_total: Optional[int], result: Dict[str, Any]) -> Optional[str]:
        """增量扫描之后，检查「新收藏在头部」这个前提还成立吗。

        :return: 前提不成立的原因；一切正常返回 ``None``。
        """
        if prev_total is None or result.get("errors"):
            return None
        remote_total = int(result.get("total_remote") or 0)
        if remote_total <= prev_total:
            return None
        if result.get("fetched") and not result.get("added"):
            return (
                "远端收藏从 %d 条涨到 %d 条，但从头部一条新收藏都没找到"
                % (prev_total, remote_total)
            )
        return None
