"""导出格式：JSON / CSV / Markdown / Excel。"""

from __future__ import annotations

import csv
import json
import os
from typing import Any, Dict, Iterable, List, Sequence

from .config import KIND_LABEL
from .models import ROW_FIELDS, CollectItem, SyncReport


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)


def write_json(path: str, payload: Dict[str, Any]) -> str:
    """写一份 JSON（UTF-8、两空格缩进、不转义中文）。"""
    _ensure_parent(path)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return path


def write_csv(path: str, items: Sequence[CollectItem], *, include_removed: bool = True) -> str:
    """写 CSV（带 UTF-8 BOM，Excel 双击不乱码）。"""
    _ensure_parent(path)
    rows = _rows(items, include_removed=include_removed)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=ROW_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def write_markdown(
    path: str,
    items: Sequence[CollectItem],
    *,
    meta: Dict[str, Any],
    include_removed: bool = False,
) -> str:
    """写一份可读的 Markdown 清单。"""
    _ensure_parent(path)
    active = [it for it in items if not it.removed]
    lines = [
        "# 拷貝漫畫（copy4000.com）我的收藏",
        "",
        "- 账号：`%s`（%s）" % (meta.get("username", "?"), meta.get("nickname", "?")),
        "- 导出时间：%s" % meta.get("exported_at", ""),
        "- 同步模式：%s" % meta.get("mode", ""),
        "- 收藏总数：**%d** 部（有效 %d）" % (len(items), len(active)),
        "- 页面：%s/web/person/shujia" % meta.get("host", ""),
        "",
        "| # | 漫画 | 作者 | 状态 | 最新话 | 上次读到 | 人气 | 链接 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]

    def esc(value: Any) -> str:
        text = str(value if value is not None else "")
        return (text or "—").replace("|", "\\|")

    for row in _rows(items, include_removed=include_removed):
        cells = {k: esc(row.get(k)) for k in ROW_FIELDS}
        cells["链接"] = row.get("链接") or "#"
        lines.append(
            "| {序号} | {漫画名称} | {作者} | {状态} | {最新话} | {上次读到} | {人气} | [打开]({链接}) |".format(
                **cells
            )
        )
    lines += ["", "> 封面图地址、章节 ID 等完整字段见同目录的 JSON 文件。", ""]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return path


def write_xlsx(path: str, items: Sequence[CollectItem], *, include_removed: bool = True) -> str:
    """写 Excel；未安装 openpyxl 时返回空字符串（调用方据此提示）。"""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        return ""

    _ensure_parent(path)
    wb = Workbook()
    ws = wb.active
    ws.title = "我的收藏"
    ws.append(ROW_FIELDS)

    head_fill = PatternFill("solid", fgColor="4472C4")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")

    for row in _rows(items, include_removed=include_removed):
        ws.append([row.get(k, "") for k in ROW_FIELDS])

    widths = {"漫画名称": 46, "作者": 24, "题材": 20, "链接": 46, "最新话链接": 46, "封面": 52}
    for idx, name in enumerate(ROW_FIELDS, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = widths.get(name, 14)
    ws.freeze_panes = "A2"
    wb.save(path)
    return path


def _rows(items: Sequence[CollectItem], *, include_removed: bool) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    source = [it for it in items if include_removed or not it.removed]
    for index, item in enumerate(source, start=1):
        out.append(item.to_row(index, kind_label=KIND_LABEL.get(item.kind, item.kind)))
    return out


#: 格式名 -> 需要的数据形态
WRITERS = ("json", "csv", "md", "xlsx")


def write_report(path: str, report: SyncReport) -> str:
    """把同步报告写成 JSON，便于外部程序消费。"""
    return write_json(path, report.to_dict())
