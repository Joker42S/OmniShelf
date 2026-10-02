"""Web 上传页：importer 下拉读注册表 + 上传文件导入 + 结果面板。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from omnishelf import repository as repo
from omnishelf.config import Settings
from omnishelf.importers.json_file import JsonFileImporter
from omnishelf.importers.registry import IMPORTERS, register
from omnishelf.web.app import create_app


@pytest.fixture()
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _movies_payload(titles: list[str]) -> bytes:
    return json.dumps(
        {"items": [{"title": t, "year": 2020, "tmdb_id": 100 + n} for n, t in enumerate(titles)]}
    ).encode("utf-8")


def test_upload_page_lists_importers_from_registry(client: TestClient) -> None:
    text = client.get("/upload").text
    options = re.findall(r'<option value="([a-z_]+)"', text)
    # 内置的四个都在（读自注册表，不是硬编码）
    assert {"copymanga", "picamanga", "json_movies", "json_books"} <= set(options)
    assert 'name="importer"' in text
    assert 'type="file"' in text  # 「添加文件」按钮
    assert "上传" in client.get("/").text  # 导航里有入口


def test_new_importer_appears_without_touching_the_page(client: TestClient) -> None:
    """新增 importer 后下拉框自动出现（证明不是硬编码的列表）。"""

    @register("unit_web_upload")
    class Dummy(JsonFileImporter):
        source = "unit_web_upload"
        kind = "game"
        mapping = {"title": ["title"]}

    try:
        text = client.get("/upload").text
        assert 'value="unit_web_upload"' in text
        assert "unit_web_upload（游戏）" in text  # 选项里带 kind 的中文名
    finally:
        IMPORTERS.pop("unit_web_upload", None)


def test_upload_imports_and_shows_result(
    client: TestClient, session: Session, settings: Settings
) -> None:
    response = client.post(
        "/upload",
        data={"importer": "json_movies"},
        files={"file": ("my_backup.json", _movies_payload(["A", "B"]), "application/json")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/upload?log_id=")
    assert "archived=imports%2Fjson_movies%2Fmy_backup.json" in location

    # 原始文件按来源存档
    archived = settings.imports_dir / "json_movies" / "my_backup.json"
    assert archived.is_file()
    assert json.loads(archived.read_text(encoding="utf-8"))["items"]

    result_page = client.get(location)
    assert result_page.status_code == 200
    assert "新增 2" in result_page.text
    assert "原始文件已存档" in result_page.text

    session.expire_all()
    assert len(repo.list_items(session, kind="movie")) == 2


def test_upload_is_idempotent_on_repeat(client: TestClient, session: Session) -> None:
    payload = _movies_payload(["A"])
    for _ in range(2):
        client.post(
            "/upload",
            data={"importer": "json_movies"},
            files={"file": ("again.json", payload, "application/json")},
            follow_redirects=False,
        )
    session.expire_all()
    assert len(repo.list_items(session, kind="movie")) == 1


def test_upload_rejects_unknown_importer(client: TestClient) -> None:
    response = client.post(
        "/upload",
        data={"importer": "nope"},
        files={"file": ("x.json", b"{}", "application/json")},
    )
    assert response.status_code == 400
    assert "未注册的 importer" in response.text


def test_upload_rejects_empty_file(client: TestClient) -> None:
    response = client.post(
        "/upload",
        data={"importer": "json_movies"},
        files={"file": ("empty.json", b"", "application/json")},
    )
    assert response.status_code == 400
    assert "文件是空的" in response.text


def test_upload_sanitizes_filename(client: TestClient, settings: Settings) -> None:
    """文件名里的路径要清掉（不能写到 imports 之外）；空格这类合法字符保留。"""
    client.post(
        "/upload",
        data={"importer": "json_movies"},
        files={"file": ("../../evil name.json", _movies_payload(["X"]), "application/json")},
        follow_redirects=False,
    )
    saved = list((settings.imports_dir / "json_movies").glob("*"))
    assert [p.name for p in saved] == ["evil name.json"]
    assert saved[0].parent == settings.imports_dir / "json_movies"


def test_upload_reports_record_errors(client: TestClient) -> None:
    """坏记录不会让整个上传失败，错误进结果面板。"""
    payload = json.dumps({"items": [{"title": "Good"}, {"year": 2000}]}).encode()
    response = client.post(
        "/upload",
        data={"importer": "json_movies"},
        files={"file": ("mixed.json", payload, "application/json")},
        follow_redirects=False,
    )
    page = client.get(response.headers["location"])
    assert "新增 1" in page.text
    assert "错误 1" in page.text
    assert "记录 2" in page.text


def test_upload_result_page_handles_missing_log(client: TestClient) -> None:
    page = client.get("/upload?log_id=999")
    assert page.status_code == 200
    assert "找不到导入记录" in page.text


@pytest.mark.parametrize(
    ("filename", "payload", "expected"),
    [
        ("broken.json", b"{ this is not json", "JSON 解析失败"),
        ("wrong.json", b'{"items": {"a": 1}}', "找不到 items/records/data/works 数组"),
        ("list.json", b'"just a string"', "顶层必须是数组或对象"),
    ],
)
def test_upload_file_level_failure_lands_in_the_log(
    client: TestClient, settings: Settings, session: Session, filename: str, payload: bytes, expected: str
) -> None:
    """选错 importer / 文件损坏：不 500；失败记进 import_logs 并在结果页显示。"""
    response = client.post(
        "/upload",
        data={"importer": "json_movies"},
        files={"file": (filename, payload, "application/json")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get(response.headers["location"])
    assert page.status_code == 200
    assert "错误 1" in page.text and "fetch 失败" in page.text and expected in page.text
    assert "共 0 条" in page.text
    # 失败的原始文件仍然留档：便于事后查「我当时传了什么」
    assert (settings.imports_dir / "json_movies" / filename).is_file()
    session.expire_all()
    assert repo.list_items(session, kind="movie") == []


def test_upload_route_falls_back_to_400_on_file_errors(
    client: TestClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """路由自己的兜底：文件级异常给 400 提示，而不是 500，也不留下垃圾存档。"""

    def boom(*args: object, **kwargs: object) -> None:
        from omnishelf.importers.base import NormalizeError

        raise NormalizeError("模拟的解析失败")

    monkeypatch.setattr("omnishelf.web.routes.upload.import_uploaded", boom)
    response = client.post(
        "/upload",
        data={"importer": "json_movies"},
        files={"file": ("nope.json", b"{}", "application/json")},
    )
    assert response.status_code == 400
    assert "没法用 json_movies 解析" in response.text
    assert not (settings.imports_dir / "json_movies" / "nope.json").exists()
