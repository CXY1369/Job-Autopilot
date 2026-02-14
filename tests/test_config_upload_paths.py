from __future__ import annotations

from autojobagent import config


def test_get_effective_upload_directories_deduplicates(monkeypatch):
    monkeypatch.setattr(
        config,
        "get_allowed_upload_directories",
        lambda: ["/a", "/b", "/b"],
        raising=True,
    )
    monkeypatch.setattr(
        config,
        "ensure_project_resume_variants_dir",
        lambda: "/b",
        raising=True,
    )

    dirs = config.get_effective_upload_directories()
    assert dirs == ["/a", "/b"]


def test_is_upload_path_allowed_inside_and_outside(monkeypatch, tmp_path):
    allowed_root = tmp_path / "allowed"
    denied_root = tmp_path / "denied"
    allowed_root.mkdir()
    denied_root.mkdir()

    inside = allowed_root / "resume.pdf"
    outside = denied_root / "resume.pdf"
    inside.write_text("ok", encoding="utf-8")
    outside.write_text("no", encoding="utf-8")

    monkeypatch.setattr(
        config,
        "get_effective_upload_directories",
        lambda: [str(allowed_root)],
        raising=True,
    )

    assert config.is_upload_path_allowed(str(inside)) is True
    assert config.is_upload_path_allowed(str(outside)) is False

