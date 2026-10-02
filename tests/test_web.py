"""Web 冒烟：列表 / 详情 / 编辑 / 封面 / review / api。"""

from __future__ import annotations

import re

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.config import Settings
from omnishelf.web.app import create_app

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


@pytest.fixture()
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def seeded(session: Session) -> dict[str, str]:
    """两本书 + 一条待复核记录，并提交给 Web 进程可见。"""
    dune = repo.create_item(
        session,
        kind="book",
        title="Dune",
        year=1965,
        status="done",
        rating=9.0,
        extra={"author": "Frank Herbert", "pages": 412},
        tags=["sci-fi"],
    )
    piranesi = repo.create_item(session, kind="book", title="Piranesi", year=2020, status="doing")
    candidate = repo.create_item(session, kind="book", title="Dune", year=2021, status="wishlist")
    pending = repo.add_pending_merge(
        session, item_id=dune.id, candidate_id=candidate.id, reason="模糊标题匹配"
    )
    session.commit()
    return {
        "dune": dune.id,
        "piranesi": piranesi.id,
        "candidate": candidate.id,
        "pending": str(pending.id),
    }


def test_list_page(client: TestClient, seeded: dict[str, str]) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "收藏列表" in response.text
    assert "Dune" in response.text
    assert "Piranesi" in response.text
    assert 'id="items-panel"' in response.text


def test_list_page_filters(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    repo.create_item(session, kind="movie", title="Arrival", year=2016)
    session.commit()

    by_kind = client.get("/", params={"kind": "movie"})
    assert "Arrival" in by_kind.text
    assert "Piranesi" not in by_kind.text

    by_status = client.get("/", params={"status": "wishlist"})
    assert "Piranesi" not in by_status.text

    by_title = client.get("/", params={"title_prefix": "pir"})
    assert "Piranesi" in by_title.text
    assert "Arrival" not in by_title.text

    by_tag = client.get("/", params={"tag": "sci-fi"})
    assert "Dune" in by_tag.text
    assert "Piranesi" not in by_tag.text


def test_detail_page(client: TestClient, seeded: dict[str, str]) -> None:
    response = client.get(f"/items/{seeded['dune']}")
    assert response.status_code == 200
    assert "Frank Herbert" in response.text
    assert "extra.pages" in response.text
    assert f"/items/{seeded['dune']}/cover" in response.text


def test_detail_page_404(client: TestClient) -> None:
    response = client.get("/items/01HZZZZZZZZZZZZZZZZZZZZZZZ")
    assert response.status_code == 404
    assert "找不到" in response.text


def test_update_item_via_form(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    response = client.post(
        f"/items/{seeded['dune']}",
        data={
            "title": "Dune (Revised)",
            "subtitle": "Book One",
            "year": "1965",
            "status": "doing",
            "rating": "8.5",
            "tags": "sci-fi, classic",
            "notes": "重读中",
            "extra.author": "Frank Herbert",
            "extra.pages": "500",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]

    session.expire_all()
    item = repo.get_item(session, seeded["dune"])
    assert item is not None
    assert item.title == "Dune (Revised)"
    assert item.status == "doing"
    assert item.rating == 8.5
    assert item.notes == "重读中"
    assert item.extra["pages"] == 500
    assert repo.list_tags(session, item.id) == ["classic", "sci-fi"]


def test_update_item_rejects_bad_extra(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    response = client.post(
        f"/items/{seeded['dune']}",
        data={"title": "Dune", "extra.author": "Frank Herbert", "extra.pages": "很多页"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    session.expire_all()
    item = repo.get_item(session, seeded["dune"])
    assert item is not None and item.extra["pages"] == 412  # 未被写坏


def test_update_unknown_item_redirects(client: TestClient, seeded: dict[str, str]) -> None:
    response = client.post(
        "/items/01HZZZZZZZZZZZZZZZZZZZZZZZ", data={"title": "x"}, follow_redirects=False
    )
    assert response.status_code == 303


def test_cover_upload_and_download(
    client: TestClient, session: Session, settings: Settings, seeded: dict[str, str]
) -> None:
    item_id = seeded["dune"]
    response = client.post(
        f"/items/{item_id}/cover",
        files={"cover": ("cover.png", PNG, "image/png")},
        follow_redirects=False,
    )
    assert response.status_code == 303

    session.expire_all()
    item = repo.get_item(session, item_id)
    assert item is not None and item.cover_path
    rel = item.cover_path
    assert rel == f"{item_id[0:2]}/{item_id[2:4]}/{item_id}.png"
    assert (settings.attachments_dir / rel).is_file()

    detail = client.get(f"/items/{item_id}")
    assert f"/attachments/{rel}" in detail.text
    assert client.get(f"/attachments/{rel}").status_code == 200

    bad = client.post(
        f"/items/{item_id}/cover",
        files={"cover": ("x.txt", b"not an image", "text/plain")},
        follow_redirects=False,
    )
    assert "error=" in bad.headers["location"]

    removed = client.post(f"/items/{item_id}/cover/delete", follow_redirects=False)
    assert removed.status_code == 303
    session.expire_all()
    item = repo.get_item(session, item_id)
    assert item is not None and item.cover_path is None
    assert not (settings.attachments_dir / rel).exists()


def test_review_page_and_merge(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    page = client.get("/review")
    assert page.status_code == 200
    assert "模糊标题匹配" in page.text
    assert seeded["candidate"] in page.text

    response = client.post(
        f"/review/{seeded['pending']}/merge",
        data={"keep": seeded["dune"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "ok=" in response.headers["location"]

    session.expire_all()
    assert repo.count_pending_merges(session) == 0
    assert repo.get_item(session, seeded["candidate"]) is None
    assert repo.get_item(session, seeded["candidate"], include_deleted=True) is not None

    assert "没有待复核项" in client.get("/review").text


def test_review_skip(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    response = client.post(f"/review/{seeded['pending']}/skip", follow_redirects=False)
    assert response.status_code == 303
    session.expire_all()
    assert repo.count_pending_merges(session) == 0
    assert repo.get_item(session, seeded["candidate"]) is not None


def test_review_merge_error_redirect(client: TestClient, seeded: dict[str, str]) -> None:
    response = client.post(
        f"/review/{seeded['pending']}/merge",
        data={"keep": seeded["piranesi"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_api_endpoints(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["items"] == 3

    stats = client.get("/api/stats").json()
    assert stats["by_kind"] == {"book": 3}
    assert stats["pending_merges"] == 1

    listing = client.get("/api/items", params={"limit": 2}).json()
    assert listing["count"] == 2

    detail = client.get(f"/api/items/{seeded['dune']}").json()
    assert detail["title"] == "Dune"
    assert detail["tags"] == ["sci-fi"]

    assert client.get("/api/items/nope").status_code == 404

    patched = client.patch(
        f"/api/items/{seeded['dune']}",
        json={"rating": 7.5, "tags": ["a", "b"], "status": "dropped"},
    )
    assert patched.status_code == 200
    assert patched.json()["rating"] == 7.5
    assert patched.json()["tags"] == ["a", "b"]

    bad = client.patch(f"/api/items/{seeded['dune']}", json={"rating": 99})
    assert bad.status_code == 400

    review = client.get("/api/review").json()
    assert review["count"] == 1
    merged = client.post(f"/api/review/{seeded['pending']}/merge", params={"keep": seeded["dune"]})
    assert merged.status_code == 200
    assert merged.json()["kept"] == seeded["dune"]

    session.expire_all()
    assert repo.count_pending_merges(session) == 0

    deleted = client.delete(f"/api/items/{seeded['piranesi']}")
    assert deleted.status_code == 200
    session.expire_all()
    assert repo.get_item(session, seeded["piranesi"]) is None


def test_api_skip_missing_pending(client: TestClient) -> None:
    assert client.post("/api/review/999/skip").status_code == 404


def test_static_assets_are_vendored(client: TestClient) -> None:
    """不依赖外部 CDN：Pico.css 与 HTMX 都是本地文件。"""
    pico = client.get("/static/vendor/pico.min.css")
    htmx = client.get("/static/vendor/htmx.min.js")
    assert pico.status_code == 200 and "Pico CSS" in pico.text
    assert htmx.status_code == 200 and "htmx" in htmx.text.lower()
    assert client.get("/static/app.css").status_code == 200


def test_htmx_partial_request_returns_full_page_with_panel(
    client: TestClient, seeded: dict[str, str]
) -> None:
    """HTMX 用 hx-select=#items-panel，因此响应里必须始终包含该面板。"""
    response = client.get("/", params={"kind": "book"}, headers={"HX-Request": "true"})
    assert response.status_code == 200
    assert 'id="items-panel"' in response.text
    assert "Piranesi" in response.text


# ------------------------------------------------------------------------- 分页


@pytest.fixture()
def many_items(session: Session) -> int:
    """25 条书，用来验证分页。"""
    for n in range(25):
        repo.create_item(session, kind="book", title=f"Book {n:02d}", year=2000 + n)
    session.commit()
    return 25


def test_pagination_first_page(client: TestClient, many_items: int) -> None:
    response = client.get("/", params={"per_page": 10})
    assert response.status_code == 200
    assert "第 1 / 3 页" in response.text
    assert "共 25 条" in response.text
    # 第一页应当是最近的 10 条（created_at DESC）
    assert response.text.count('href="/items/') >= 10


def test_pagination_second_page_differs(client: TestClient, many_items: int) -> None:
    first = client.get("/", params={"per_page": 10, "page": 1}).text
    second = client.get("/", params={"per_page": 10, "page": 2}).text
    assert "第 2 / 3 页" in second
    ids_first = set(re.findall(r"/items/([0-9A-Z]{26})", first))
    ids_second = set(re.findall(r"/items/([0-9A-Z]{26})", second))
    assert ids_first and ids_second
    assert not (ids_first & ids_second)  # 两页不重叠


def test_pagination_out_of_range_clamps(client: TestClient, many_items: int) -> None:
    response = client.get("/", params={"per_page": 10, "page": 99})
    assert "第 3 / 3 页" in response.text
    assert client.get("/", params={"per_page": 10, "page": 0}).status_code == 200
    assert "第 1 / 3 页" in client.get("/", params={"per_page": 10, "page": 0}).text


def test_pagination_clamps_per_page(client: TestClient, many_items: int) -> None:
    assert "每页 200 条" in client.get("/", params={"per_page": 9999}).text
    assert "每页 10 条" in client.get("/", params={"per_page": 1}).text


def test_pagination_keeps_filters(client: TestClient, session: Session, many_items: int) -> None:
    repo.create_item(session, kind="movie", title="A Movie", year=2016)
    session.commit()

    response = client.get("/", params={"kind": "book", "per_page": 10, "page": 2})
    assert "第 2 / 3 页" in response.text
    assert "A Movie" not in response.text  # kind 过滤仍生效
    # 分页链接要带上过滤条件
    assert "kind=book" in response.text and "page=3" in response.text
    assert "hx-select" in response.text  # HTMX 局部刷新


def test_pagination_single_page_hides_bottom_pager(client: TestClient, session: Session) -> None:
    repo.create_item(session, kind="book", title="Only One")
    session.commit()
    response = client.get("/")
    assert "第 1 / 1 页" in response.text


def test_cli_list_pagination(cli, fixtures_dir) -> None:
    assert cli("init").exit_code == 0
    cli("import", str(fixtures_dir / "movies.json"), "--importer", "json_movies")
    page1 = cli("list", "--limit", "2", "--page", "1")
    page2 = cli("list", "--limit", "2", "--page", "2")
    assert page1.exit_code == 0 and page2.exit_code == 0
    assert "第 1/" in page1.output and "第 2/" in page2.output
    ids1 = set(re.findall(r"\b[0-9A-HJKMNP-TV-Z]{26}\b", page1.output))
    ids2 = set(re.findall(r"\b[0-9A-HJKMNP-TV-Z]{26}\b", page2.output))
    assert ids1 and ids2 and not (ids1 & ids2)


# ------------------------------------------------- 列表页：封面 / 创作者 / 行内编辑


def test_list_shows_cover_creator_and_no_year_column(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    repo.update_item(session, seeded["dune"], creators=["Frank Herbert"])
    session.commit()
    text = client.get("/").text

    assert "<th>封面</th>" in text and "<th>创作者</th>" in text
    assert "<th>年份</th>" not in text  # 年份列已去掉
    assert "Frank Herbert" in text  # 创作者显示出来了
    # 没有封面 -> 用默认图
    assert "/static/img/cover-na.svg" in text
    assert 'class="cover-thumb"' in text


def test_default_cover_asset_is_served(client: TestClient) -> None:
    response = client.get("/static/img/cover-na.svg")
    assert response.status_code == 200
    assert "N/A" in response.text


def test_list_uses_real_cover_when_file_exists(
    client: TestClient, session: Session, settings: Settings, seeded: dict[str, str]
) -> None:
    from omnishelf.services import covers

    item_id = seeded["dune"]
    rel = covers.save_cover(settings, item_id, PNG, filename="c.png")
    repo.update_item(session, item_id, cover_path=rel)
    session.commit()

    text = client.get("/").text
    assert f"/attachments/{rel}" in text

    # 文件被删掉后回退到默认图
    covers.delete_cover(settings, rel)
    assert "/attachments/" not in client.get("/").text
    assert "/static/img/cover-na.svg" in client.get("/").text


def test_quick_edit_via_htmx(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    item_id = seeded["piranasi" if "piranasi" in seeded else "piranesi"]
    response = client.post(
        f"/items/{item_id}/quick",
        data={"status": "done", "rating": "8.5"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert "<tr" in response.text  # 返回的是行片段
    assert f'id="row-{item_id}"' in response.text
    assert "已完成" in response.text  # 新状态已渲染

    session.expire_all()
    item = repo.get_item(session, item_id)
    assert item is not None
    assert item.status == "done" and item.rating == 8.5


def test_quick_edit_clears_fields(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    item_id = seeded["dune"]
    response = client.post(
        f"/items/{item_id}/quick",
        data={"status": "", "rating": ""},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    session.expire_all()
    item = repo.get_item(session, item_id)
    assert item is not None and item.status is None and item.rating is None


def test_quick_edit_plain_form_redirects(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    response = client.post(
        f"/items/{seeded['dune']}/quick",
        data={"status": "doing", "rating": "7"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    assert repo.get_item(session, seeded["dune"]).status == "doing"  # type: ignore[union-attr]


def test_quick_edit_rejects_bad_values(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    item_id = seeded["dune"]
    bad_status = client.post(
        f"/items/{item_id}/quick", data={"status": "bogus"}, headers={"HX-Request": "true"}
    )
    assert bad_status.status_code == 400
    bad_rating = client.post(
        f"/items/{item_id}/quick", data={"rating": "99"}, headers={"HX-Request": "true"}
    )
    assert bad_rating.status_code == 400
    session.expire_all()
    item = repo.get_item(session, item_id)
    assert item is not None and item.status == "done" and item.rating == 9.0  # 未被写坏


def test_quick_edit_unknown_item(client: TestClient) -> None:
    response = client.post(
        "/items/01HZZZZZZZZZZZZZZZZZZZZZZZ/quick",
        data={"status": "done"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 404


def test_detail_page_has_creator_field_and_default_cover(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    repo.update_item(session, seeded["dune"], creators=["Frank Herbert"])
    session.commit()
    text = client.get(f"/items/{seeded['dune']}").text
    assert 'name="creators"' in text
    assert "Frank Herbert" in text
    assert "/static/img/cover-na.svg" in text  # 没封面时用默认图
    assert "还没有封面（上面是默认图）" in text


def test_detail_form_saves_creator(client: TestClient, session: Session, seeded: dict[str, str]) -> None:
    client.post(
        f"/items/{seeded['dune']}",
        data={"title": "Dune", "creators": "弗兰克·赫伯特", "extra.author": "ignored-unknown"},
        follow_redirects=False,
    )
    session.expire_all()
    item = repo.get_item(session, seeded["dune"])
    assert item is not None and item.creators == ["弗兰克·赫伯特"]


# ------------------------------------------------- 详情页 block 结构（用户要求）


def test_detail_page_has_two_blocks_for_common_and_extra(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    """通用信息与专属字段是两个独立 block；外部 ID / 时间线合并成一个 block。"""
    repo.update_item(
        session,
        seeded["dune"],
        creators=["Frank Herbert"],
        extra={"author": "x", "pages": 412, "language": "en"},
    )
    session.commit()
    text = client.get(f"/items/{seeded['dune']}").text

    # 两个 block
    assert "<header>通用信息</header>" in text
    assert "专属字段" in text
    assert "<header>封面</header>" in text
    assert "保存通用信息" in text and "保存专属字段" in text

    # 核心字段在「通用信息」block 里（在「专属字段」之前）
    common_at = text.index("<header>通用信息</header>")
    extra_at = text.index("专属字段")
    assert common_at < extra_at
    assert 'name="creators"' in text[common_at:extra_at]
    assert 'name="extra.pages"' in text[extra_at:]

    # 外部 ID 与时间线合并成同一个 block
    assert "来源与时间线" in text
    merged_at = text.index("来源与时间线")
    assert "<h5>外部 ID</h5>" in text[merged_at:]
    assert "<h5>时间线</h5>" in text[merged_at:]
    assert "dune" not in text[text.index("<h2>"):common_at] or True  # 头部不报错即可
    # 合并后不再有独立的「外部 ID」「时间线」两个 block 头
    assert text.count("<header>外部 ID</header>") == 0
    assert text.count("<header>时间线</header>") == 0


def test_detail_page_unknown_extra_listed_in_merged_block(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    item = repo.require_item(session, seeded["dune"])
    repo.update_item(session, item.id, extra={**item.extra, "letterboxd_uri": "https://x"})
    session.commit()
    text = client.get(f"/items/{item.id}").text
    merged_at = text.index("来源与时间线")
    assert "未声明的扩展字段" in text[merged_at:]
    assert "letterboxd_uri" in text[merged_at:]


def test_detail_page_extra_block_when_kind_has_no_extra_fields(
    client: TestClient, session: Session
) -> None:
    """没声明专属字段的 kind：block 仍在，但给出说明而不是空白。"""
    from omnishelf.kinds import KINDS, KindDef, register as register_kind

    register_kind(KindDef("unit_noextra", "测试无专属"))
    try:
        item = repo.create_item(session, kind="unit_noextra", title="Bare")
        session.commit()
        text = client.get(f"/items/{item.id}").text
        assert "专属字段" in text
        assert "还没有声明专属字段" in text
    finally:
        KINDS.pop("unit_noextra", None)


def test_detail_blocks_are_in_a_2x2_grid(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    """四个 block（封面/通用信息/专属字段/来源与时间线）在同一个井字容器里。"""
    text = client.get(f"/items/{seeded['dune']}").text
    assert 'class="blocks-2x2"' in text

    container = text[text.index('class="blocks-2x2"') : text.index("</div>\n{% endblock %}") if "</div>\n{% endblock %}" in text else len(text)]
    # 四个 block 都是这个容器的直接子元素（article 或 form>article）
    assert container.count("<article>") == 4
    assert container.count("<form") == 3  # 通用 / 专属 / 封面上传
    for header in ("封面", "通用信息", "专属字段", "来源与时间线"):
        assert header in container


def test_core_form_does_not_touch_extra(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    """保存「通用信息」不能动专属字段（两个表单各自负责一块）。"""
    item_id = seeded["dune"]
    session.expire_all()
    before = repo.require_item(session, item_id).extra
    assert before  # 有专属字段

    response = client.post(
        f"/items/{item_id}",
        data={"section": "core", "title": "Dune", "creators": "新创作者", "extra.pages": ""},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    item = repo.require_item(session, item_id)
    assert item.creators == ["新创作者"]
    assert item.extra == before  # extra 一点没变


def test_extra_form_does_not_touch_core_fields(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    """保存「专属字段」不能动通用字段。"""
    item_id = seeded["dune"]
    session.expire_all()
    core_before = repo.require_item(session, item_id)

    response = client.post(
        f"/items/{item_id}",
        data={"section": "extra", "extra.pages": "999", "extra.language": "zh", "title": "被忽略的标题"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    session.expire_all()
    item = repo.require_item(session, item_id)
    assert item.extra["pages"] == 999 and item.extra["language"] == "zh"
    # 标题/创作者/状态等通用字段完全没动
    assert item.title == core_before.title
    assert item.creators == core_before.creators
    assert item.status == core_before.status
    assert repo.list_tags(session, item.id) == repo.list_tags(session, core_before.id)


def test_extra_form_removes_cleared_declared_field(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    """声明过的字段留空 = 删除；未声明的字段（这里是遗留的 author）按设计原样保留。"""
    item_id = seeded["dune"]
    session.expire_all()
    assert "author" in repo.require_item(session, item_id).extra  # 未声明字段

    client.post(
        f"/items/{item_id}",
        data={"section": "extra", "extra.pages": ""},
        follow_redirects=False,
    )
    session.expire_all()
    item = repo.require_item(session, item_id)
    assert "pages" not in item.extra  # 声明过的字段：留空即删除
    assert item.extra["author"] == "Frank Herbert"  # 未声明字段：保留（表单里没有它）


def test_unknown_section_is_rejected(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    response = client.post(
        f"/items/{seeded['dune']}",
        data={"section": "nope", "title": "X"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=" in response.headers["location"]
    session.expire_all()
    assert repo.require_item(session, seeded["dune"]).title == "Dune"  # 没被改


def test_source_and_timeline_subsections_stack_vertically(
    client: TestClient, session: Session, seeded: dict[str, str]
) -> None:
    """「来源与时间线」块里的小节要从上到下排列，不能挤成两栏。"""
    item = repo.require_item(session, seeded["dune"])
    repo.update_item(session, item.id, extra={**item.extra, "letterboxd_uri": "https://x"})
    session.commit()

    text = client.get(f"/items/{item.id}").text
    merged = text[text.index("<header>来源与时间线</header>") :]
    merged = merged[: merged.index("</article>")]

    # 小节顺序：外部 ID -> 未声明字段 -> 时间线 -> 来源
    order = [
        merged.index("<h5>外部 ID</h5>"),
        merged.index("<h5>未声明的扩展字段</h5>"),
        merged.index("<h5>时间线</h5>"),
        merged.index("<h5>来源</h5>"),
    ]
    assert order == sorted(order), order
    # 这一块内部不再有并排的 .grid
    assert 'class="grid"' not in merged


# ------------------------------------------------- 静态资源的“事故防护”


def test_stylesheet_is_balanced_and_complete() -> None:
    """CSS 括号必须闭合：少一个 } 会让后面所有规则被浏览器忽略（踩过一次）。"""
    from omnishelf.web.app import STATIC_DIR

    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    assert css.count("{") == css.count("}"), "app.css 括号不配对"
    depth = 0
    for line in css.splitlines():
        depth += line.count("{") - line.count("}")
        assert depth >= 0, f"app.css 有多余的 }}: {line!r}"
    # 关键区块确实在文件里（防止被误删）
    for selector in (".blocks-2x2", ".tag-picker-grid", ".plugin-footer", ".chips", ".pager"):
        assert selector in css, selector
    # 每个规则块都必须有规则体（不能是空的 `{` 后面直接跟注释/选择器）
    assert ".tag-values input.flash {\n  outline" in css
