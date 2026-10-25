#!/usr/bin/env bash
# 同步所有来源（豆瓣 / ASMR.ONE / 拷贝漫画 / 哔咔 / Steam，不含本地音乐），首尾各备份一次 data/。
#
#   scripts/sync-all.sh              增量同步（默认）：每个来源 fetch + import 一步到位
#   scripts/sync-all.sh --full       全量重扫：会翻完整个列表，只有这条路才数得出「取消收藏」
#   scripts/sync-all.sh --dry-run    只抓不写库（插件照常落盘），结尾不做备份提交
#
# --full 和 --dry-run 可以一起用（--full --dry-run），参数顺序无所谓。
# 全量比增量慢得多（豆瓣 9 张列表整段重抓、哔咔 200 多页、拷贝漫画整架翻完），别在赶时间时用。
#
# 凭据一律走 plugins_data/<插件>/config.toml 里存好的那份，命令行不出现 token / cookie / api_key。
# 漫画两边（拷贝漫画 + 哔咔）反过来：**优先用口令登录**，不指望缓存的 token。口令从
#   MANGA_PASSWORD=<口令> scripts/sync-all.sh        （推荐，不进 shell 历史）
# 读，没给环境变量且在交互终端时开头问一次（输入不回显）。两处共用同一个口令。
#   账号名/邮箱用配置里存的那份，命令行不重复写。账号没有口令时插件就照旧用配置里的 token。
#   注意口令会以参数形式短暂出现在进程列表里；插件打码后落日志，不会写进配置文件。
# 代理是各插件自己的参数（picamanga / steam 都有 --proxy，值也可以存在各自配置里），脚本不再单独注入。
# 关键参数一律显式写（--mode / --incremental / --media / --status / --lists），不依赖出厂默认。
# 注意：picamanga / copymanga 会把本次生效的 mode 写回自己的配置，所以跑完 --full 之后
#       配置里就是 full；下次想增量记得显式带 --mode incremental（本脚本会自己带）。
#       steam_fetch 的命令行不写回配置，而且它压根没有增量/全量之分——每次都整份重抓，
#       靠 importer 的 (steam, appid) 判重把已入库的算成「未变化」，商店信息走 7 天本地缓存。
# Steam 第一次抓某个 --cc 区域会明显慢（商店信息只能逐款问）；本机已在配置里存好区域就别再传。
# Steam 的导入行不打印「跳过 / 待复核」，待复核看结尾那份统计。
# ASMR.ONE 只同步私人收藏：播放列表整份翻完要几十分钟，收益不比收藏。
# 退出码 0 不代表抓到了：插件整页被拒时会标「不完整」/停止原因 fetch-error，那时入库的是上一次的
#   状态库（看着像「未变化 N 条」其实什么也没扫）。脚本把这种情况算失败并提醒重跑。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

OMNISHELF=".venv/bin/omnishelf"
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }

DRY_RUN=0
SYNC_MODE=incremental
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --full) SYNC_MODE=full ;;
        help|-h|--help)
            awk 'NR == 1 { next } /^#/ { print; next } { exit }' "${BASH_SOURCE[0]}"
            exit 0 ;;
        *) log "不认识的参数：${arg}（可用：--full / --dry-run / --help）"; exit 2 ;;
    esac
done

# 豆瓣/ASMR.ONE 用布尔开关，哔咔/拷贝漫画用 --mode，两边都跟着 SYNC_MODE 走
if [[ "$SYNC_MODE" == "full" ]]; then
    INCR_FLAG=(--no-incremental)
    SYNC_LABEL=全量
else
    INCR_FLAG=(--incremental)
    SYNC_LABEL=增量
fi

FAILED_LIST=""
FAILED_COUNT=0
CAPTURE_CODE=0
CAPTURE_OUTPUT=""

note_failure() {
    FAILED_COUNT=$(( FAILED_COUNT + 1 ))
    FAILED_LIST="$FAILED_LIST    - $1"$'\n'
}

# run_capture <命令…>：原样打印输出，同时把退出码和文本留在 CAPTURE_CODE / CAPTURE_OUTPUT
run_capture() {
    local output code
    output="$("$@" 2>&1)"
    code=$?
    [[ -n "$output" ]] && printf '%s\n' "$output"
    CAPTURE_CODE="$code"
    CAPTURE_OUTPUT="$output"
    return "$code"
}

data_is_dirty() { [[ -n "$(git -C data status --porcelain)" ]]; }

commit_data() {
    local msg="$1"
    if ! data_is_dirty; then
        log "data/ 工作区干净，跳过提交"
        return 0
    fi
    log "data/ 有未提交变动，先备份：$msg"
    if ! "$OMNISHELF" data-commit -m "$msg"; then
        log "✗ data-commit 失败，中止后续步骤（别让没备份的写入继续叠加）"
        exit 1
    fi
}

# run_sync <人看的名字> <插件名> <命令> [参数…]
run_sync() {
    local label="$1" plugin="$2" command="$3"
    shift 3
    local args=(plugin "$plugin" "$command" "$@")
    (( DRY_RUN )) && args+=(--dry-run)

    local tmp
    tmp="$(mktemp)"
    log "→ ${label}"
    "${OMNISHELF}" "${args[@]}" 2>&1 | tee "$tmp"
    local code=${PIPESTATUS[0]}

    if (( code != 0 )); then
        log "✗ ${label} 失败（退出码 ${code}），继续跑后面的来源"
        note_failure "$label"
        rm -f "$tmp"
        return 1
    fi
    # 退出码 0 不等于抓到了：整页被拒/失败时插件会标「不完整」，停止原因写成 fetch-error。
    # 那种情况下入库的是上一次的状态库，看着「未变化 N 条」其实这趟什么都没扫。
    if grep -qE "不完整|fetch-error" "$tmp"; then
        log "⚠ ${label} 退出码是 0，但这趟没抓全（见上面「不完整」那行）——不算同步完成，得重跑"
        note_failure "${label}（退出 0 但没抓全）"
        rm -f "$tmp"
        return 1
    fi
    log "✓ $label"
    rm -f "$tmp"
    return 0
}

# 漫画两边的口令：MANGA_PASSWORD 环境变量优先，没给又在交互终端就开头问一次（哔咔/拷贝漫画共用）。
# 留空 = 这趟不带口令，插件照旧用配置里存的 token。口令只作为参数出现在这一次命令里，
# 插件打码后才落日志、不会写进配置文件。
MANGA_PASSWORD="${MANGA_PASSWORD:-}"
manga_password_asked=0
ensure_manga_password() {
    (( manga_password_asked )) && return 0
    manga_password_asked=1
    if [[ -n "$MANGA_PASSWORD" ]]; then
        log "漫画口令：用 MANGA_PASSWORD 里给的那份"
        return 0
    fi
    if [[ -t 0 ]]; then
        printf '[%s] 哔咔/拷贝漫画 的账号口令（输入不回显、不会被保存；直接回车 = 只用配置里存的 token）: ' "$(date +%H:%M:%S)"
        IFS= read -rs MANGA_PASSWORD
        printf '\n'
    else
        log "没给 MANGA_PASSWORD，这里也不是交互终端：漫画两边只用配置里存的 token"
    fi
}

# Steam：一次抓游戏库 + 愿望单两份快照，插件自己各导一次（共用 importer steam）
sync_steam() {
    local label="Steam 游戏库/愿望单"
    local args=(plugin steam_fetch sync --lists all)
    (( DRY_RUN )) && args+=(--dry-run)

    log "→ ${label}（steamid / cc / proxy 都用配置里存的）"
    if ! run_capture "${OMNISHELF}" "${args[@]}"; then
        log "✗ ${label} 失败（退出码 ${CAPTURE_CODE}），继续跑后面的来源"
        note_failure "$label"
        return 1
    fi

    log "✓ $label"
    # 退出码 0 不代表两份都抓到了：空响应/私密档案会报「未写快照」，那一份根本没有文件可入库
    if [[ "$CAPTURE_OUTPUT" == *"未写快照"* ]]; then
        log "  ⚠ 有列表被 Steam 报成「游戏信息不可用或未公开」，那一份没落快照，本次没导入它"
        log "  ⚠ 多半是档案私密或接口抖动，不是真没有——要确认就单独重跑一次这条命令"
    fi
    return 0
}

[[ -x "$OMNISHELF" ]] || { log "✗ 找不到可执行的 ${OMNISHELF}，先在仓库根跑 uv sync --dev"; exit 1; }
[[ -d data/.git ]] || { log "✗ data/ 不是独立 git 仓库，检查 --data-dir 设置"; exit 1; }

log "环境自查：$("$OMNISHELF" -V 2>/dev/null || echo "omnishelf 起不来")"
log "同步方式：${SYNC_LABEL}（${SYNC_MODE}）—— 全量才会扫出「取消收藏」"
log "同步前的库统计"
"$OMNISHELF" plugin kind_chart top || log "（统计没拿到，不影响同步）"

# 1) 先把同步之前就存在的改动存一份，保证同步产生的写入是干净的一层
commit_data "同步前快照：$(date +'%F %H:%M')"

# 2) 豆瓣：3 个媒体 × 3 张列表
for media in movie book game; do
    for status in collect do wish; do
        run_sync "豆瓣 ${media}/${status}" douban_fetch fetch_import \
            --media "$media" --status "$status" "${INCR_FLAG[@]}"
    done
done

# 3) ASMR.ONE：只同步私人收藏（播放列表整份翻完要几十分钟，不跑）
run_sync "ASMR.ONE 私人收藏" asmr_playlist fetch_import --target favorites "${INCR_FLAG[@]}"

# 4) 漫画两边：mode 显式写（留空会照配置里上一次的 mode 走）；账号名用配置里存的，认证优先给口令
ensure_manga_password
if [[ -n "$MANGA_PASSWORD" ]]; then
    log "  漫画两边带 --password 跑：配置里的 token 被站点踢时，插件会当场用账号口令重新登录换新 token"
else
    log "  漫画两边这次不给口令，只用配置里存的 token"
fi
run_sync "拷贝漫画书架" copymanga_fetch fetch_import --mode "$SYNC_MODE" --password "$MANGA_PASSWORD"
run_sync "哔咔收藏夹" picamanga_fetch fetch_import --mode "$SYNC_MODE" --password "$MANGA_PASSWORD"

# 5) Steam：整份重抓 + 两份快照各自入库（没有增量开关）
sync_steam

# 6) 收尾：统计 + 备份
log "同步后的库统计"
"$OMNISHELF" plugin kind_chart top || log "（统计没拿到）"

if (( DRY_RUN )); then
    log "--dry-run：没有写库，跳过同步后的备份提交"
    data_is_dirty && log "提示：data/ 仍有未提交变动（多半是插件日志），下次运行会先把它存掉"
else
    commit_data "${SYNC_LABEL}同步所有来源：$(date +'%F %H:%M')"
fi

if (( FAILED_COUNT > 0 )); then
    log "✗ 共 $FAILED_COUNT 个来源失败："
    printf '%s' "$FAILED_LIST"
    exit 1
fi
log "全部完成，用时 ${SECONDS}s"
