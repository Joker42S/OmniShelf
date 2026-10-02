"""命令行入口。

既可以 ``python -m copymanga``，也可以通过顶层的
``copymanga_favorites_export.py`` 运行。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from .config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_HOST,
    FORMATS,
    MODES,
    ORDERINGS,
    ExportConfig,
    load_config,
)
from .errors import AuthError, ConfigError, CopymangaError
from .exporter import FavoritesExporter

EPILOG = """\
token 获取方式（任选其一）：
  1. 浏览器登录 https://copy4000.com 后按 F12 →
     Application/应用 → Storage/存储 → Cookies → https://copy4000.com
     → 复制名为 token 的那一项的值（40 位十六进制）
  2. 在浏览器控制台执行：document.cookie.match(/token=([^;]+)/)[1]
  3. 直接让脚本用账号密码换：--username 你的用户名 --password 你的密码
  4. 环境变量：COPYMANGA_TOKEN / COPYMANGA_USERNAME + COPYMANGA_PASSWORD

示例：
  # 增量刷新（快，只请求头部，默认模式）
  python -m copymanga

  # 全量重建，并标记已取消收藏的条目
  python -m copymanga --mode full

  # 额外导出 CSV 和 Excel
  python -m copymanga --format json,csv,xlsx

  # 指定域名与数据目录
  python -m copymanga --host https://copy4000.com --data-dir ./data
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="copymanga",
        description="copymanga —— 导出拷貝漫畫（copy4000.com）个人收藏，支持增量/全量同步",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    g = p.add_argument_group("站点与模式")
    g.add_argument("--host", default=None, help="网站域名，默认 %s" % DEFAULT_HOST)
    g.add_argument(
        "--mode",
        default=None,
        choices=MODES,
        help="incremental=增量（默认，只翻头部）；full=全量（遍历并标记取消收藏）",
    )
    g.add_argument(
        "--ordering",
        default=None,
        choices=ORDERINGS,
        help="收藏列表排序，默认 -datetime_modifier（按加到書架時間降序）。"
             "增量模式只允许这个值，因为只有它保证新收藏在头部",
    )
    g.add_argument(
        "--no-enforce-ordering",
        action="store_true",
        help="关闭排序不变量校验（不建议：关闭后排序口径变化会导致增量静默漏数据）",
    )

    g = p.add_argument_group("认证")
    g.add_argument("--token", default=None, help="登录 token（Cookie 里的 token 值）")
    g.add_argument("--cookie", default=None, help="整条 Cookie 字符串，自动提取 token")
    g.add_argument("--username", default=None, help="用户名（无 token 时用于换取 token）")
    g.add_argument("--password", default=None, help="密码（无 token 时用于换取 token）")
    g.add_argument(
        "--no-persist-token",
        action="store_true",
        help="token 自动续期后不写回配置文件（默认会写回，避免每次运行都重新登录）",
    )

    g = p.add_argument_group("存储与输出")
    g.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="配置文件路径")
    g.add_argument("--data-dir", default=None, help="数据目录，默认 <项目>/data")
    g.add_argument("--store", default=None, help="状态库文件名，默认 copymanga_favorites.json")
    g.add_argument(
        "--format",
        dest="formats",
        default=None,
        help="导出格式，逗号分隔，可选 %s，默认 json" % "/".join(FORMATS),
    )
    g.add_argument("--no-snapshot", action="store_true", help="不额外导出 JSON 快照")
    g.add_argument("--no-raw", action="store_true", help="JSON 里不保留接口原始返回（体积更小）")
    g.add_argument("--report-json", action="store_true", help="把本次同步报告以 JSON 打到 stdout")

    g = p.add_argument_group("请求行为")
    g.add_argument("--page-size", type=int, default=None, help="每页条数，默认 100")
    g.add_argument("--timeout", type=float, default=None, help="单次请求超时秒数，默认 40")
    g.add_argument("--retries", type=int, default=None, help="失败重试次数，默认 3")
    g.add_argument("--delay", dest="request_delay", type=float, default=None, help="翻页间隔秒数，默认 0.3")
    g.add_argument("--kinds", default=None, help="收藏类型，逗号分隔，默认 comics,cartoons")
    g.add_argument("--free-types", default=None, help="接口分组，逗号分隔，默认 1,2")
    g.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验")
    g.add_argument("-q", "--quiet", action="store_true", help="静默模式")
    return p


def _overrides_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    """把命令行参数转成配置覆盖项（None 的一律不覆盖）。"""
    return {
        "host": args.host,
        "mode": args.mode,
        "ordering": args.ordering,
        "enforce_ordering": False if args.no_enforce_ordering else None,
        "token": args.token,
        "cookie": args.cookie,
        "username": args.username,
        "password": args.password,
        "persist_token": False if args.no_persist_token else None,
        "data_dir": args.data_dir,
        "store_name": args.store,
        "page_size": args.page_size,
        "timeout": args.timeout,
        "retries": args.retries,
        "request_delay": args.request_delay,
        "kinds": args.kinds,
        "free_types": args.free_types,
        "formats": args.formats,
        "verify_ssl": False if args.insecure else None,
        "snapshot": False if args.no_snapshot else None,
        "include_raw": False if args.no_raw else None,
        "quiet": True if args.quiet else None,
    }


def main(argv: Optional[List[str]] = None) -> int:
    """命令行主函数。

    :param argv: 参数列表，``None`` 表示取 ``sys.argv[1:]``。
    :return: 进程退出码（0 成功，1 配置/认证错误，2 接口错误）。
    """
    args = build_parser().parse_args(argv)

    try:
        config = load_config(
            path=args.config,
            overrides=_overrides_from_args(args),
        )
    except ConfigError as exc:
        print("配置错误：%s" % exc, file=sys.stderr)
        return 1

    if not config.quiet:
        print("拷貝漫畫收藏导出 | %s | 模式 %s | 排序 %s | 数据目录 %s"
              % (config.base_url, config.mode, config.ordering,
                 os.path.abspath(config.data_dir)))

    exporter = FavoritesExporter(config=config)
    try:
        report = exporter.run()
    except AuthError as exc:
        print("认证失败：%s" % exc, file=sys.stderr)
        return 1
    except CopymangaError as exc:
        print("接口错误：%s" % exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130

    if args.report_json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    elif not config.quiet:
        print("")
        print(report)
        print("状态库：%s" % report.store_path)
        for path in report.files:
            print("  导出：%s" % path)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
