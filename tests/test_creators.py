"""创作者（多值）的切分与归一化规则。

都是从真实数据里踩到过的情况（口径见 ``docs/development.md`` 的「字段口径」一节）：

* 一个字符串里**括号外**的分隔符 = 多个创作者（``"K8on,mh2en"``、
  ``"くろきわたる / 枩葉松"``、``"色谷あすか  /  葉乃はるか"``）；
* **括号里**的分隔符 = 团体成员名单，属于名字本身
  （``"Current Storage (momi, ぴょん吉)"``、``"STANKY (楓子、yozo)"``）；
* 斜杠两侧没有空白 = 一个名字（``"horn/wood"``、``"ミニチュアキマイラ1/24 (J-2型)"``）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.importers.registry import get, load_builtin_importers
from omnishelf.repository import clean_creators, split_creators


# ------------------------------------------------------------------ 切分规则


def test_split_on_commas_outside_brackets() -> None:
    assert split_creators("K8on,mh2en") == ["K8on", "mh2en"]
    assert split_creators("稲光伸二,或十せねか,Rusty Soul") == ["稲光伸二", "或十せねか", "Rusty Soul"]
    assert split_creators("一日三尻，不倒翁，灰背隼") == ["一日三尻", "不倒翁", "灰背隼"]
    assert split_creators("雨宮ミズキ, Pin-Point") == ["雨宮ミズキ", "Pin-Point"]
    assert split_creators("大島智, 大島永遠 (スイートピー＆COCOA BREAK)") == [
        "大島智", "大島永遠 (スイートピー＆COCOA BREAK)"
    ]


def test_split_on_slash_with_spaces() -> None:
    assert split_creators("くろきわたる / 枩葉松") == ["くろきわたる", "枩葉松"]
    assert split_creators("色谷あすか  /  葉乃はるか  /  shimazaki  /  ヒバ") == [
        "色谷あすか", "葉乃はるか", "shimazaki", "ヒバ"
    ]
    # 组合：括号外的斜杠 + 括号里的逗号
    assert split_creators("A (x, y) / B") == ["A (x, y)", "B"]


def test_brackets_protect_inner_separators() -> None:
    for value in (
        "Current Storage (momi, ぴょん吉)",
        "アジサイデンデン (川上六角, 小鳥遊レイ)",
        "スイートピー＆COCOA BREAK (大島智, 大島永遠)",
        "STANKY (楓子、yozo)",
        "新生ロリショタ (新生フロンティア) (shuffle、kozi)",
        "ぷぅのぷぅぷぅぷぅ(ぷぅ崎ぷぅ奈, しんどう)",
        "凭依る抜け道 【あるべんと, 條仔】",
        "Group [a, b]",
        "社团（甲，乙）",
    ):
        assert split_creators(value) == [value], value


def test_bare_slash_is_not_a_separator() -> None:
    for value in ("horn/wood", "ミニチュアキマイラ1/24 (J-2型)", "N/A", "A/B/C"):
        assert split_creators(value) == [value], value


def test_clean_creators_normalizes_and_dedupes() -> None:
    assert clean_creators("A, B") == ["A", "B"]
    assert clean_creators("A，B / C") == ["A", "B", "C"]
    assert clean_creators([" A ", "A", "", "B"]) == ["A", "B"]
    assert clean_creators(["A / B", "B"]) == ["A", "B"]  # 去重且保序
    assert clean_creators(None) == []
    assert clean_creators("   ") == []
    assert repo.clean_creators_from_form(["A", "B"]) == ["A", "B"]
    assert repo.clean_creators_from_form("K8on,mh2en") == ["K8on", "mh2en"]


def test_clean_creators_is_applied_on_write(session: Session) -> None:
    item = repo.create_item(session, kind="asmr", title="T", creators="A / B, C")
    assert item.creators == ["A", "B", "C"]
    updated = repo.update_item(session, item.id, creators="くろきわたる / 枩葉松")
    assert updated is not None and updated.creators == ["くろきわたる", "枩葉松"]
    kept = repo.update_item(session, item.id, creators=["Current Storage (momi, ぴょん吉)"])
    assert kept is not None and kept.creators == ["Current Storage (momi, ぴょん吉)"]


# --------------------------------------------------------------- importer 产出


def test_picamanga_author_string_is_split(tmp_path: Path) -> None:
    """picamanga 的 ``author`` 是**一个字符串**，里面的多作者要拆开。

    用真实形状的夹具改 author 字段（这样改字段名/结构时测试会跟着发现）。
    """
    load_builtin_importers()
    payload = json.loads((Path(__file__).parent / "fixtures" / "picamanga.json").read_text("utf-8"))
    authors = [
        "K8on,mh2en",                              # 括号外的逗号 -> 两个
        "Current Storage (momi, ぴょん吉)",          # 括号里的逗号 -> 一个
        "色谷あすか  /  葉乃はるか  /  shimazaki",     # 括号外的斜杠（多空格）-> 三个
        "STANKY (楓子、yozo)",                       # 顿号 -> 一个
        "horn/wood",                               # 无空格斜杠 -> 一个
    ]
    for item, author in zip(payload["items"], authors):
        item["author"] = author
    path = tmp_path / "picamanga.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    importer_cls: Any = get("picamanga")
    importer = importer_cls(path)
    got = [importer.normalize(raw).creators for raw in importer.fetch()]
    assert got[:4] == [
        ["K8on", "mh2en"],
        ["Current Storage (momi, ぴょん吉)"],
        ["色谷あすか", "葉乃はるか", "shimazaki"],
        ["STANKY (楓子、yozo)"],
    ]
    assert got[4] == ["horn/wood"]


def test_copymanga_authors_array_stays_multiple() -> None:
    """copymanga 的 ``authors`` 本来就是数组：一个作者一个元素（**不要再拼字符串**）。"""
    load_builtin_importers()
    importer_cls: Any = get("copymanga")
    importer = importer_cls(Path(__file__).parent / "fixtures" / "copymanga.json")
    drafts = [importer.normalize(raw) for raw in importer.fetch()]
    assert any(len(draft.creators) > 1 for draft in drafts)
    for draft in drafts:
        assert all(" / " not in name for name in draft.creators), draft.creators
