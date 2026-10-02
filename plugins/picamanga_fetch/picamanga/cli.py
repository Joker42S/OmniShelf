"""命令行入口。

``python -m picamanga`` 即可运行（在插件目录里跑会把真实收藏写进 ``<插件>/data/``，
建议用 ``--data-dir`` 指定别处的目录）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional

from .config import (
    BASE_URL,
    DEFAULT_CONSECUTIVE_KNOWN_STOP,
    MODES,
    NEWEST_FIRST_SORT,
    SORTS,
    load_config,
)
from .errors import AuthError, ConfigError, PicamangaError
from .exporter import FavoritesExporter

EPILOG = """\
认证信息获取方式（任选其一）：
  1. 直接让工具用邮箱 + 口令换令牌：--email 你的账号 --password 你的口令
     （口令只用于换令牌，本工具不会把它写进任何文件）
  2. 抓包 App/网页的请求头 authorization，复制其值：--token <token>
  3. 环境变量：PICAMANGA_TOKEN / PICAMANGA_EMAIL + PICAMANGA_PASSWORD
     （另有 PICAMANGA_HOST / PICAMANGA_PROXY / PICAMANGA_DATA_DIR 等，见 config.ENV_MAP）

示例：
  # 增量刷新（快，只翻头部，默认模式）
  python -m picamanga

  # 全量重建，并标记已取消收藏的条目
  python -m picamanga --mode full

  # 指定账号、数据目录，并且不在状态库里留接口原始返回
  python -m picamanga --email me@example.com --data-dir ./backup --no-raw

  # 本机能直连但被风控/需要走代理（比如本地起了一个 HTTP 代理）
  python -m picamanga --proxy http://127.0.0.1:10809
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="picamanga",
        description="picamanga —— 导出哔咔（picacomic）个人收藏夹，支持增量/全量同步",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    g = p.add_argument_group("站点与模式")
    g.add_argument("--host", default=None, help="接口地址，默认 %s" % BASE_URL)
    g.add_argument(
        "--mode",
        default=None,
        choices=MODES,
        help="incremental=增量（默认，只翻头部）；full=全量（遍历所有页并标记取消收藏）",
    )
    g.add_argument(
        "--sort",
        default=None,
        choices=sorted(SORTS),
        help="收藏列表排序，默认 %s（按收藏日期新到旧）。"
             "增量模式只允许这个值，因为只有它保证新收藏在头部" % NEWEST_FIRST_SORT,
    )
    g.add_argument(
        "--no-enforce-sort",
        action="store_true",
        help="关闭「新收藏在头部」的校验（不建议：前提失效时增量会静默漏数据）",
    )

    g = p.add_argument_group("认证")
    g.add_argument("--token", default=None, help="登录令牌（请求头 authorization 的值）")
    g.add_argument("--email", default=None, help="账号邮箱（无 token 时用于换 token）")
    g.add_argument("--password", default=None, help="口令（无 token 时用于换 token，不会被保存）")

    g = p.add_argument_group("存储与输出")
    g.add_argument("--data-dir", default=None, help="数据目录，默认 <项目>/data")
    g.add_argument("--store", default=None, help="状态库文件名，默认 picamanga_favorites.json")
    g.add_argument("--no-raw", action="store_true", help="状态库里不保留接口原始返回（体积更小）")
    g.add_argument("--report-json", action="store_true", help="把本次同步报告以 JSON 打到 stdout")

    g = p.add_argument_group("请求行为")
    g.add_argument(
        "--max-pages",
        default=None,
        help="单次最多翻多少页（防呆），默认一直翻到最后一页",
    )
    g.add_argument("--delay", dest="request_delay", type=float, default=None,
                   help="翻页间隔秒数，默认 0.15")
    g.add_argument("--timeout", type=float, default=None, help="单次请求超时秒数，默认 30")
    g.add_argument("--retries", type=int, default=None, help="失败重试次数，默认 3")
    g.add_argument(
        "--consecutive-known-stop",
        type=int,
        default=None,
        help="增量扫描连续命中多少条本地已有收藏就停手，默认 %d"
             % DEFAULT_CONSECUTIVE_KNOWN_STOP,
    )
    g.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验")
    g.add_argument(
        "--proxy",
        default=None,
        help="代理服务器地址，如 http://127.0.0.1:10809（只写 域名:端口 也行，按 HTTP 代理对待）。"
             "默认不指定：这种情况下 urllib 仍会认 HTTPS_PROXY 之类的环境变量",
    )
    g.add_argument("-q", "--quiet", action="store_true", help="静默模式")
    return p


def _overrides_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    """把命令行参数转成配置覆盖项（None 的一律不覆盖）。"""
    return {
        "host": args.host,
        "mode": args.mode,
        "sort": args.sort,
        "enforce_sort": False if args.no_enforce_sort else None,
        "token": args.token,
        "email": args.email,
        "password": args.password,
        "data_dir": args.data_dir,
        "store_name": args.store,
        "max_pages": args.max_pages,
        "timeout": args.timeout,
        "retries": args.retries,
        "request_delay": args.request_delay,
        "consecutive_known_stop": args.consecutive_known_stop,
        "proxy": args.proxy,
        "verify_ssl": False if args.insecure else None,
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
        config = load_config(overrides=_overrides_from_args(args))
    except ConfigError as exc:
        print("配置错误：%s" % exc, file=sys.stderr)
        return 1

    if not config.quiet:
        print("哔咔收藏夹导出 | %s | 模式 %s | 排序 %s | 数据目录 %s"
              % (config.base_url, config.mode, config.sort,
                 os.path.abspath(config.data_dir)))
        if config.proxy_target:
            print("  走代理：%s" % config.proxy_target)

    exporter = FavoritesExporter(config=config)
    try:
        report = exporter.sync()
    except AuthError as exc:
        print("认证失败：%s" % exc, file=sys.stderr)
        return 1
    except PicamangaError as exc:
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
        for message in report.errors:
            print("  ! %s" % message)

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
