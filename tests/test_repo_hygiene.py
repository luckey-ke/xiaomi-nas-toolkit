#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""仓库卫生检查。专门为"公开仓库"而写，防止真实设备信息被误提交。

检查项：
  1. 代码与文档里没有残留真实设备信息（IP / UID / MAC / 密码 / 证书编号）
  2. config/config.example.json 结构合法，且 host / uid 为空
  3. .gitignore 覆盖了 config/config.json 与 vendor/
  4. keep_plugins_v4.py 能正确解析 plugins.json，并拒绝非法配置

敏感串在本文件里用拼接方式构造，避免测试自己把自己判为泄漏。

用法：python -X utf8 tests/test_repo_hygiene.py
"""
import importlib.util
import json
import os
import pathlib
import re
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent

SECRETS = [
    "192.168." + "1.67",          # 参考设备的局域网 IP
    "9817" + "60641",             # 参考设备的米家 UID
    "D4-53" + "-2A",              # 参考设备 MAC 前缀
    "yueke" + "088097",           # 参考设备部署时用过的密码
    "1220308746",                 # 证书文件名里的随机段
]

SKIP_DIRS = {".git", "vendor", "stage", "__pycache__", ".idea", ".vscode", "logs"}
TEXT_SUFFIXES = {".py", ".sh", ".ps1", ".cmd", ".json", ".md", ".txt", ".example", ".gitignore"}


def iter_text_files():
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.name == "config.json":
            continue  # 本地配置，已被 .gitignore 排除
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name != ".gitignore":
            continue
        if path.resolve() == pathlib.Path(__file__).resolve():
            continue  # 本文件里是拼接后的串，不参与扫描
        yield path


def test_no_secrets():
    hits = []
    for path in iter_text_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for secret in SECRETS:
            if secret in text:
                hits.append("%s 含 %s" % (path.relative_to(ROOT), secret[:6] + "***"))
    assert not hits, "发现真实设备信息残留：\n  " + "\n  ".join(hits)
    print("[1/4] 无真实设备信息残留  OK")


def test_no_private_ip():
    """文档里可以出现示例 IP，但代码里不应硬编码任何具体内网地址。"""
    pattern = re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b")
    bad = []
    for path in iter_text_files():
        if path.suffix.lower() not in {".py", ".sh", ".ps1", ".cmd"}:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in pattern.findall(text):
            bad.append("%s: %s" % (path.relative_to(ROOT), match))
    assert not bad, "代码里硬编码了内网地址（应走配置）：\n  " + "\n  ".join(bad)
    print("[2/4] 代码里无硬编码内网地址  OK")


def test_example_config():
    path = ROOT / "config" / "config.example.json"
    assert path.is_file(), "缺少 config/config.example.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    assert cfg["device"]["host"] == "", "示例配置的 host 必须为空"
    assert cfg["device"]["uid"] == "", "示例配置的 uid 必须为空"
    assert cfg["device"]["shell_port"] == 2323
    assert cfg["access"]["ssh_port"] == 22
    enabled = cfg["plugins"]["enabled"]
    assert enabled, "示例配置应至少启用一个插件"
    for pid in enabled:
        assert pid in cfg["plugins"]["ports"], "插件 %s 缺少端口配置" % pid
        assert isinstance(cfg["plugins"]["ports"][pid], int), "插件 %s 端口必须是整数" % pid
    assert cfg["selfheal"]["mode"] in ("calendar", "interval")
    print("[3/4] 示例配置结构合法  OK")


def test_gitignore():
    path = ROOT / ".gitignore"
    assert path.is_file(), "缺少 .gitignore"
    text = path.read_text(encoding="utf-8")
    for needle in ("config/config.json", "vendor/", "*.pem"):
        assert needle in text, ".gitignore 未覆盖 %s" % needle
    print("[4/4] .gitignore 覆盖本地配置/上游代码/私钥  OK")


def load_keep_module():
    path = ROOT / "core" / "keep_plugins_v4.py"
    spec = importlib.util.spec_from_file_location("keep_plugins_v4_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_keep_config_parsing():
    mod = load_keep_module()
    with tempfile.TemporaryDirectory() as temp:
        good = pathlib.Path(temp) / "plugins.json"
        good.write_text(json.dumps({
            "uid": "100000001",
            "plugins": {"alpha": 9301, "beta": "9302"},
            "allowed_ips": ["10.0.0.0/24"],
        }), encoding="utf-8")
        mod.CONF_PATH = good
        cfg = mod.load_config()
        assert cfg is not None, "合法配置应解析成功"
        assert cfg.uid == "100000001"
        assert cfg.user == "u100000001"
        assert cfg.plugins == {"alpha": 9301, "beta": 9302}, cfg.plugins
        assert cfg.allowed_ips == ["10.0.0.0/24"]
        assert cfg.list_file.as_posix().endswith("/data/plugin/u100000001.list"), cfg.list_file

        bad = pathlib.Path(temp) / "bad.json"
        bad.write_text(json.dumps({"uid": "1", "plugins": {"alpha": "not-a-port"}}), encoding="utf-8")
        mod.CONF_PATH = bad
        assert mod.load_config() is None, "非法端口应被拒绝"

        missing = pathlib.Path(temp) / "nope.json"
        mod.CONF_PATH = missing
        assert mod.load_config() is None, "配置缺失应被拒绝"
    print("[额外] 自愈脚本配置解析  OK")


def main():
    test_no_secrets()
    test_no_private_ip()
    test_example_config()
    test_gitignore()
    test_keep_config_parsing()
    print("\nREPO HYGIENE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
