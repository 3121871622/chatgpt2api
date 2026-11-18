import json
import time
from pathlib import Path

import pytest

from services import update_service as update_module
from services.update_service import (
    UpdateService,
    _release_notes,
    parse_version,
)


@pytest.fixture()
def service(tmp_path, monkeypatch):
    version_file = tmp_path / "VERSION"
    version_file.write_text("1.9.21\n", encoding="utf-8")
    monkeypatch.setattr(update_module, "_VERSION_FILE", version_file)
    monkeypatch.setattr(update_module, "DATA_DIR", tmp_path)
    monkeypatch.setenv("QINGMENG_UPDATE_REPO", "3121871622/chatgpt2api")
    monkeypatch.delenv("QINGMENG_UPDATE_ENABLED", raising=False)
    return UpdateService()


def _write_updater_state(tmp_path: Path, phase: str = "idle") -> None:
    directory = tmp_path / "update"
    directory.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    (directory / "state.json").write_text(
        json.dumps({
            "phase": phase,
            "target_version": "",
            "current_version": "1.9.21",
            "message": "",
            "updated_at": stamp,
            "finished_at": "",
        }),
        encoding="utf-8",
    )


def test_parse_version_accepts_tag_and_plain():
    assert parse_version("v1.9.21") == (1, 9, 21)
    assert parse_version("1.10.0") == (1, 10, 0)
    assert parse_version("nightly") is None
    assert parse_version("") is None


def test_release_notes_extracts_section():
    changelog = "# 日志\n\n## [1.9.22] 2026-11-19 03:00\n\n### 新增\n- [新增] 更新中心\n\n## [1.9.21] 2026-11-19 02:10\n- [修复] 旧\n"
    notes = _release_notes(changelog, "1.9.22")
    assert "更新中心" in notes
    assert "1.9.21" not in notes


def test_check_reports_newer_github_tag(service, tmp_path, monkeypatch):
    monkeypatch.setattr(
        update_module,
        "_http_json",
        lambda url: [{"name": "v1.9.22"}, {"name": "v1.9.21"}, {"name": "v1.9.9"}],
    )
    monkeypatch.setattr(
        update_module,
        "_http_text",
        lambda url: "## [1.9.22] 2026-11-19 03:00\n### 新增\n- [新增] 更新中心\n",
    )
    result = service.check(force=True)
    assert result["current_version"] == "1.9.21"
    assert result["latest_version"] == "1.9.22"
    assert result["update_available"] is True
    assert "更新中心" in result["notes"]
    assert result["release_url"].endswith("/v1.9.22")


def test_check_is_latest_when_versions_equal(service, monkeypatch):
    monkeypatch.setattr(update_module, "_http_json", lambda url: [{"name": "v1.9.21"}])
    result = service.check(force=True)
    assert result["latest_version"] == "1.9.21"
    assert result["update_available"] is False


def test_check_reports_github_failure(service, monkeypatch):
    import urllib.error

    def boom(url):
        raise urllib.error.URLError("network down")

    monkeypatch.setattr(update_module, "_http_json", boom)
    result = service.check(force=True)
    assert result["update_available"] is False
    assert "GitHub 版本查询失败" in result["error"]


def test_apply_requires_fresh_updater(service):
    with pytest.raises(ValueError, match="更新代理未安装"):
        service.apply("1.9.22")


def test_apply_rejects_old_or_invalid_version(service, tmp_path):
    _write_updater_state(tmp_path)
    with pytest.raises(ValueError, match="格式"):
        service.apply("latest")
    with pytest.raises(ValueError, match="不高于当前版本"):
        service.apply("1.9.20")


def test_apply_writes_request_for_newer_version(service, tmp_path):
    _write_updater_state(tmp_path)
    result = service.apply("1.9.22", requested_by="admin")
    assert result["submitted"] is True
    request = json.loads((tmp_path / "update" / "request.json").read_text(encoding="utf-8"))
    assert request["target_version"] == "1.9.22"
    assert request["image"] == "ghcr.io/3121871622/chatgpt2api:1.9.22"
    assert service.apply("1.9.23")["submitted"] is False


def test_status_reports_agent_heartbeat(service, tmp_path):
    _write_updater_state(tmp_path, phase="pulling")
    status = service.status()
    assert status["updater"]["phase"] == "pulling"
    assert status["updater"]["updater_available"] is True

def test_status_accepts_agent_epoch_heartbeat(service, tmp_path):
    """The host agent runs in UTC; container runs in Asia/Shanghai. The epoch
    field must win so a live agent is not reported as offline."""
    directory = tmp_path / "update"
    directory.mkdir(exist_ok=True)
    (directory / "state.json").write_text(
        json.dumps({
            "phase": "idle",
            "target_version": "",
            "current_version": "1.9.22",
            "message": "",
            "updated_at": "2026-11-18 19:00:00",
            "updated_epoch": int(time.time()),
            "finished_at": "",
        }),
        encoding="utf-8",
    )
    assert service.status()["updater"]["updater_available"] is True


def test_status_accepts_utc_text_heartbeat(service, tmp_path):
    directory = tmp_path / "update"
    directory.mkdir(exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
    (directory / "state.json").write_text(
        json.dumps({
            "phase": "idle",
            "target_version": "",
            "current_version": "1.9.22",
            "message": "",
            "updated_at": stamp,
            "finished_at": "",
        }),
        encoding="utf-8",
    )
    assert service.status()["updater"]["updater_available"] is True