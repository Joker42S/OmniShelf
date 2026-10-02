"""同步编排：全量 / 增量两种模式 + 导出。

排序与增量的正确性
------------------

接口的排序由 ``ordering`` 决定，站点下拉框提供三个选项：

====================== ========================== ==========================
``ordering``          含义                       能否用于增量
====================== ========================== ==========================
``-datetime_modifier`` **按加到書架時間降序**       ✅ 唯一安全选项（默认）
``-datetime_updated``  按作品更新时间降序          ❌ 新收藏不一定在头部
``-datetime_browse``   按阅读时间排序              ❌ 同上
====================== ========================== ==========================

.. warning::
   实测服务端对非法 ``ordering`` 值**静默退回默认排序**、不报错，
   所以配置层做了白名单校验，并禁止增量模式搭配其它排序。

在此之上，同步过程还会实时校验两条不变量（``enforce_ordering``）：

1. **单调性**：一页之内以及跨页之间，收藏 ID 必须严格递减。
   收藏 ID 是与「加到書架時間」严格同构的自增序列，实测 392 条全量数据上严格单调。
2. **头部水位线**：第一页第一项的收藏 ID 必须 ``>=`` 本地水位线
   （现存条目里的最大收藏 ID）。排序正确时，最新的收藏必然在头部。

任一条被打破，说明排序口径变了，继续用「翻到旧条目就停」会**静默漏数据**，
因此自动降级为全量扫描并在报告里标记 ``ordering_verified=false``。

两种模式的区别
--------------

``full``（全量）
    遍历远端所有页，重建数据集；本地有、远端没有的条目标记为
    ``removed=true``（即已取消收藏），不会删除，方便回溯。

``incremental``（增量）
    接口按加到書架時間倒序返回，所以新收藏一定在头部。从第 0 页开始翻，
    一旦整页收藏 ID 都不超过水位线就立即停止，日常刷新通常只需 1 次请求。
    已存在条目的字段（最新话、上次读到、人气等）会在这条路径上顺带刷新。
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .client import CopymangaClient
from .config import FORMATS, MODES, SHELF_ORDERING, ExportConfig, KIND_LABEL
from .errors import ConfigError
from .formats import write_csv, write_json, write_markdown, write_report, write_xlsx
from .models import CollectItem, SyncReport, now_iso
from .store import JsonStore

STOP_COMPLETE = "complete"
STOP_KNOWN_BOUNDARY = "known-boundary"
STOP_WATERMARK = "watermark-reached"
STOP_DUPLICATE_GROUP = "duplicate-group"
STOP_MAX_PAGES = "max-pages"
STOP_ORDERING_BROKEN = "ordering-broken"


class FavoritesExporter:
    """把「拉取收藏 → 合并到本地 → 导出文件」串起来的门面类。

    用法一（自己给配置）::

        exporter = FavoritesExporter(config=ExportConfig(token="...", mode="incremental"))
        report = exporter.run()

    用法二（复用已有 client）::

        client = CopymangaClient(token="...")
        exporter = FavoritesExporter(client)
        report = exporter.run(mode="full")

    :param client: 可选的 :class:`~copymanga.client.CopymangaClient`，省略则按 config 新建。
    :param config: 可选的 :class:`~copymanga.config.ExportConfig`。
    :param overrides: 直接覆盖配置字段。
    """

    def __init__(
        self,
        client: Optional[CopymangaClient] = None,
        config: Optional[ExportConfig] = None,
        **overrides: Any,
    ) -> None:
        if config is None:
            config = client.config if client is not None else ExportConfig()
        if overrides:
            config = config.merge(**overrides)
        self.config = config.validate()
        self.client = client or CopymangaClient(self.config)
        self.client.config = self.config  # 让分页/超时等行为保持一致
        if self.client.token:
            self.config = self.config.merge(token=self.client.token)

    # ------------------------------------------------------------------
    # 便捷入口
    # ------------------------------------------------------------------

    def run(self, mode: Optional[str] = None, formats: Optional[Iterable[str]] = None) -> SyncReport:
        """同步 + 导出，一步到位。

        :param mode: ``"full"`` 或 ``"incremental"``，省略用配置里的。
        :param formats: 覆盖导出格式，例如 ``["json", "csv"]``。
        :return: 含 ``files`` 列表的 :class:`~copymanga.models.SyncReport`。
        """
        report = self.sync(mode=mode)
        report.files = self.export(report, formats=formats)
        return report

    def load(self) -> List[CollectItem]:
        """只读本地状态库，不联网。"""
        return list(JsonStore(self.config.store_path).load())

    # ------------------------------------------------------------------
    # 同步
    # ------------------------------------------------------------------

    def sync(self, mode: Optional[str] = None) -> SyncReport:
        """执行一次同步并写入状态库 JSON。

        增量模式下会实时校验排序不变量；一旦被打破会自动降级成全量扫描
        （报告里 ``ordering_verified=False``、``fell_back_to_full=True``），
        宁可多跑几个请求也不静默漏数据。

        :param mode: ``"full"`` 或 ``"incremental"``，省略用配置里的。
        :return: :class:`~copymanga.models.SyncReport`
        """
        cfg = self.config
        requested_mode = (mode or cfg.mode or "incremental").lower()
        if requested_mode not in MODES:
            raise ConfigError("mode 只能是 %s" % " / ".join(MODES))
        if requested_mode == "incremental" and cfg.ordering != SHELF_ORDERING:
            raise ConfigError(
                "增量模式必须使用 ordering=%r（按加到書架時間降序），当前是 %r"
                % (SHELF_ORDERING, cfg.ordering)
            )

        log = (lambda *a: None) if cfg.quiet else print
        started_at = now_iso()
        t0 = time.time()

        self.client.ensure_authenticated()
        user = self.client.user or {}
        log("· 已登录：%s（%s）" % (user.get("username", "?"), user.get("nickname", "?")))

        store = JsonStore(cfg.store_path).load()
        known_ids: Set[str] = store.ids()
        watermark = store.watermark() if requested_mode == "incremental" else None
        log("· 本地已有 %d 条 | 模式：%s | 排序：%s%s"
            % (len(known_ids), requested_mode, cfg.ordering,
               "" if watermark is None else " | 水位线 %d" % watermark))

        # 先按请求的模式跑；发现排序异常时改用 full 重跑一遍
        mode = requested_mode
        result = self._scan(mode=mode, store=store, known_ids=known_ids,
                            watermark=watermark, log=log)

        fell_back = False
        if result["ordering_broken"] and cfg.enforce_ordering and mode == "incremental":
            log("· ⚠ 排序不变量校验失败（%s），自动降级为全量扫描以免漏数据"
                % result["ordering_broken"])
            store2 = JsonStore(cfg.store_path).load()
            result = self._scan(mode="full", store=store2, known_ids=store2.ids(),
                                watermark=None, log=log)
            result["stop_reason"] = STOP_ORDERING_BROKEN
            mode = "full"
            fell_back = True
            store = store2

        removed = store.mark_removed(result["seen"]) if mode == "full" else 0

        # 按收藏 ID 重算书架位次（0 = 最新加入書架）
        top = store.reindex_shelf()
        final_watermark = store.watermark()

        finished_at = now_iso()
        store.meta.update(
            {
                "source": "%s/web/person/shujia" % cfg.base_url,
                "host": cfg.base_url,
                "user": {
                    "user_id": user.get("user_id"),
                    "username": user.get("username"),
                    "nickname": user.get("nickname"),
                    "avatar": user.get("avatar"),
                    "datetime_created": user.get("datetime_created"),
                },
                "updated_at": finished_at,
                "last_%s_sync" % mode: finished_at,
                "schema_version": JsonStore.SCHEMA_VERSION,
                "created_at": store.meta.get("created_at") or finished_at,
                "ordering": cfg.ordering,
                "shelf_watermark": final_watermark,
                "shelf_top_position": top,
            }
        )
        store.save(include_raw=cfg.include_raw)

        report = SyncReport(
            mode=mode,
            host=cfg.base_url,
            ordering=cfg.ordering,
            started_at=started_at,
            finished_at=finished_at,
            duration_s=time.time() - t0,
            user=store.meta.get("user", {}),
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
            watermark=watermark,
            ordering_verified=not result["ordering_broken"],
            fell_back_to_full=fell_back,
            store_path=cfg.store_path,
            files=[],
        )
        return report

    # ------------------------------------------------------------------
    # 一次扫描（full / incremental 共用）
    # ------------------------------------------------------------------

    def _scan(
        self,
        *,
        mode: str,
        store: JsonStore,
        known_ids: Set[str],
        watermark: Optional[int],
        log,
    ) -> Dict[str, Any]:
        """翻页扫描一遍远端，把结果合并进 ``store``。

        返回一个 dict：计数、页数、停止原因，以及排序不变量是否被打破。
        注意这里**不写盘**，由 :meth:`sync` 决定是接受还是降级重跑。
        """
        cfg = self.config
        counts = {"added": 0, "updated": 0, "unchanged": 0, "restored": 0}
        seen: Set[str] = set()
        pages = 0
        fetched = 0
        total_remote = 0
        stop_reason = STOP_COMPLETE
        boundary = False

        # 排序不变量状态
        ordering_broken: Optional[str] = None
        prev_id: Optional[int] = None
        head_id: Optional[int] = None

        for kind in cfg.kinds:
            if boundary:
                break
            for group_index, free_type in enumerate(cfg.free_types):
                if boundary:
                    break
                offset = 0
                page_no = 0
                while True:
                    if cfg.max_pages is not None and page_no >= cfg.max_pages:
                        stop_reason, boundary = STOP_MAX_PAGES, True
                        break

                    rows, total = self.client.fetch_collect_page(
                        kind, free_type, offset, cfg.page_size
                    )
                    pages += 1
                    page_no += 1
                    total_remote = max(total_remote, total)
                    if not rows:
                        break

                    ids = [str(r.get("uuid")) for r in rows]
                    numeric = self._numeric_ids(ids)
                    fresh = [cid for cid in ids if cid not in seen]

                    # 免费站 free_type=1/2 返回同一批数据：整页都是本次已见过的，直接跳过
                    if offset == 0 and group_index > 0 and not fresh:
                        stop_reason = STOP_DUPLICATE_GROUP
                        break

                    # ---- 排序不变量校验 ----
                    if cfg.enforce_ordering and ordering_broken is None and numeric is not None:
                        ordering_broken = self._check_ordering(
                            numeric, prev_id=prev_id, head_id=head_id, watermark=watermark
                        )
                        if numeric:
                            if head_id is None:
                                head_id = numeric[0]
                            prev_id = numeric[-1]
                        if ordering_broken:
                            log("· ⚠ %s" % ordering_broken)

                    for row in rows:
                        cid = str(row.get("uuid"))
                        if cid in seen:
                            continue
                        seen.add(cid)
                        fetched += 1
                        item = CollectItem.from_api(
                            row, host=cfg.base_url, free_type=free_type, kind=kind
                        )
                        counts[store.upsert(item)] += 1

                    offset += len(rows)

                    # ---- 增量停止条件 ----
                    if mode == "incremental":
                        stopped = False
                        # 主条件：整页收藏 ID 都没超过水位线 → 头部之后不可能有新收藏
                        if watermark is not None and numeric is not None:
                            if not any(value > watermark for value in numeric):
                                stop_reason, boundary, stopped = STOP_WATERMARK, True, True
                                log("· 增量：第 %d 页已无高于水位线(%s)的收藏，安全停止"
                                    % (page_no, watermark))
                        # 兜底条件：水位线缺失（老库/非数字 ID）时退回「整页都是旧条目」
                        if not stopped and all(cid in known_ids for cid in ids):
                            stop_reason, boundary, stopped = STOP_KNOWN_BOUNDARY, True, True
                            log("· 增量命中旧条目边界（第 %d 页），提前结束" % page_no)
                        if stopped:
                            break

                    if total and offset >= total:
                        break
                    if len(rows) < cfg.page_size:
                        break
                    if cfg.request_delay:
                        time.sleep(cfg.request_delay)

                if stop_reason == STOP_DUPLICATE_GROUP:
                    stop_reason = STOP_COMPLETE
                    log("· %s 的 free_type=%s 与上一组重复，跳过"
                        % (KIND_LABEL.get(kind, kind), free_type))
                    continue

        return {
            **counts,
            "seen": seen,
            "pages": pages,
            "fetched": fetched,
            "total_remote": total_remote,
            "stop_reason": stop_reason,
            "ordering_broken": ordering_broken,
        }

    # ------------------------------------------------------------------
    # 排序不变量
    # ------------------------------------------------------------------

    @staticmethod
    def _numeric_ids(ids: List[str]) -> Optional[List[int]]:
        """把收藏 ID 转成 int；只要有一个不是数字就返回 ``None``（放弃校验）。"""
        try:
            return [int(i) for i in ids]
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _check_ordering(
        numeric: List[int],
        *,
        prev_id: Optional[int],
        head_id: Optional[int],
        watermark: Optional[int],
    ) -> Optional[str]:
        """校验「按加到書架時間降序」这一不变量。

        :return: 违反原因；一切正常返回 ``None``。
        """
        for i in range(len(numeric) - 1):
            if numeric[i] <= numeric[i + 1]:
                return (
                    "第 %d/%d 项收藏 ID 未严格递减（%s -> %s），"
                    "排序口径可能已变"
                    % (i + 1, len(numeric), numeric[i], numeric[i + 1])
                )
        if prev_id is not None and numeric and numeric[0] >= prev_id:
            return "跨页收藏 ID 未延续递减（上一页末 %s，本页首 %s）" % (prev_id, numeric[0])
        if head_id is None and watermark is not None and numeric and numeric[0] < watermark:
            return (
                "头部收藏 ID %s 低于本地水位线 %s —— "
                "说明存在更新的收藏却不在头部" % (numeric[0], watermark)
            )
        return None

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------

    def export(
        self,
        report: Optional[SyncReport] = None,
        *,
        formats: Optional[Iterable[str]] = None,
        stamp: Optional[str] = None,
    ) -> List[str]:
        """把状态库导出成若干格式的文件。

        JSON 始终是「带时间戳的完整快照」；CSV / Markdown / Excel 由 ``formats`` 决定。
        ``config.snapshot`` 为真时，即使 ``formats`` 里没有 json 也会额外导出一份。

        :return: 实际写出的文件路径列表。
        """
        cfg = self.config
        requested = list(formats) if formats is not None else list(cfg.formats)
        if cfg.snapshot and "json" not in requested:
            requested.append("json")

        bad = [f for f in requested if f not in FORMATS]
        if bad:
            raise ConfigError("不支持的导出格式 %s，可选 %s" % (bad, list(FORMATS)))

        store = JsonStore(cfg.store_path).load()
        items = sorted(list(store), key=lambda it: (it.removed, it.shelf_position, it.name))
        stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
        base = os.path.join(cfg.export_dir, "copymanga_收藏_%s" % stamp)
        os.makedirs(cfg.export_dir, exist_ok=True)

        meta = {
            "host": cfg.base_url,
            "username": (store.meta.get("user") or {}).get("username", "?"),
            "nickname": (store.meta.get("user") or {}).get("nickname", "?"),
            "exported_at": now_iso(),
            "mode": report.mode if report else "",
        }

        written: List[str] = []
        if "json" in requested:
            payload = store.to_payload(include_raw=cfg.include_raw)
            payload["export"] = meta
            if report is not None:
                payload["last_sync_report"] = report.to_dict()
            written.append(write_json(base + ".json", payload))
        if "csv" in requested:
            written.append(write_csv(base + ".csv", items))
        if "md" in requested:
            written.append(write_markdown(base + ".md", items, meta=meta))
        if "xlsx" in requested:
            path = write_xlsx(base + ".xlsx", items)
            if path:
                written.append(path)
            elif not cfg.quiet:
                print("  ! 未安装 openpyxl，跳过 xlsx 导出（pip install openpyxl）")

        if report is not None:
            written.append(write_report(base + "_同步报告.json", report))

        # 报告文件排最后，便于阅读
        return written
