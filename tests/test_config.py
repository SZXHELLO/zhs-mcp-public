"""配置：相对路径以项目目录为基准、显式配置文件不存在时报错、示例配置可移植。"""
from pathlib import Path

import pytest

from zhs_exam_mcp.config import PROJECT_DIR, load_config


def _cfg_file(tmp_path, text=""):
    p = tmp_path / "elsewhere" / "my.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_default_runtime_dir_is_in_project(tmp_path):
    cfg = load_config(_cfg_file(tmp_path))
    assert cfg.runtime_dir == (PROJECT_DIR / "runtime").resolve()


def test_relative_paths_resolve_against_project_not_config(tmp_path):
    cfg = load_config(_cfg_file(tmp_path, "runtime_dir: data/rt\n"))
    assert cfg.runtime_dir == (PROJECT_DIR / "data" / "rt").resolve()


def test_missing_explicit_config_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("ZHS_CONFIG", str(tmp_path / "nope.yaml"))
    with pytest.raises(FileNotFoundError):
        load_config()


def test_example_config_is_portable():
    ex = PROJECT_DIR / "config.example.yaml"
    text = ex.read_text(encoding="utf-8")
    assert ":/" not in text.replace("http://", "").replace("https://", "")  # 无盘符绝对路径
    cfg = load_config(ex)
    assert cfg.extract_mode == "api"
