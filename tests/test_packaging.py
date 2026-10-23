"""AstrBot 插件市场规范合规检查（离线、可直接跑）。

对应规范来源：
  - 插件开发指南 / 元数据字段（docs.astrbot.app/dev/star/plugin-new.html）
  - 发布插件（docs.astrbot.app/dev/star/plugin-publish.html）
  - 插件市场 JSON 规范 2026-06-27（plugin_id = author/name、repo 形态、16MB 上限）
  - 插件国际化（.astrbot-plugin/i18n/*.json）
  - 宿主加载器实际校验（astrbot/core/star/updater.py: PLUGIN_METADATA_REQUIRED_FIELDS）

这些断言都是"提交前必须成立"的硬约束，改坏了会直接被插件市场或加载器拒掉。
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    import yaml
except ImportError:  # pragma: no cover - yaml 由 AstrBot 自带
    yaml = None

try:
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version
except ImportError:  # pragma: no cover
    SpecifierSet = None
    Version = None

REPO_URL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+(?:\.git|/tree/[A-Za-z0-9_-]+)?$"
)
SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")
REQUIRED_METADATA_FIELDS = ("name", "desc", "version", "author")
MAX_ZIP_BYTES = 16 * 1024 * 1024
MAX_METADATA_BYTES = 1024 * 1024


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


class TestPackageLayout(unittest.TestCase):
    """仓库根必须就是插件包根，否则 market / url 安装装不上。"""

    def test_required_files_at_repo_root(self):
        for name in ("metadata.yaml", "main.py", "_conf_schema.json", "requirements.txt", "README.md"):
            self.assertTrue(
                os.path.isfile(os.path.join(_ROOT, name)),
                f"仓库根缺少 {name}（AstrBot 只认插件包根下的这几个文件）",
            )

    def test_no_extra_package_level_nesting(self):
        """不允许再套一层同名目录：加载器只在包根找 main.py / <dirname>.py。"""
        nested = os.path.join(_ROOT, "astrbot_plugin_bambu_a1")
        self.assertFalse(os.path.exists(nested), "插件文件应平铺在仓库根，不要再套一层目录")

    def test_main_defines_star_subclass(self):
        source = read(os.path.join(_ROOT, "main.py"))
        self.assertRegex(source, r"class\s+\w+\(Star\)", "main.py 里必须有继承 Star 的插件类")

    def test_config_schema_is_valid_json(self):
        schema = json.loads(read(os.path.join(_ROOT, "_conf_schema.json")))
        self.assertIsInstance(schema, dict)
        for key, meta in schema.items():
            self.assertIsInstance(meta, dict, f"配置项 {key} 必须是对象")
            self.assertIn("type", meta, f"配置项 {key} 缺少 type")

    def test_i18n_files_in_expected_place(self):
        i18n_dir = os.path.join(_ROOT, ".astrbot-plugin", "i18n")
        self.assertTrue(os.path.isdir(i18n_dir), "国际化文案应放在 .astrbot-plugin/i18n/")
        files = [f for f in os.listdir(i18n_dir) if f.endswith(".json")]
        self.assertIn("zh-CN.json", files)
        self.assertIn("en-US.json", files)
        for name in files:
            payload = json.loads(read(os.path.join(i18n_dir, name)))
            self.assertIsInstance(payload, dict)
            self.assertIn("metadata", payload, f"{name} 应包含 metadata 段")
            self.assertIn("display_name", payload["metadata"])

    def test_i18n_config_keys_exist_in_schema(self):
        schema = json.loads(read(os.path.join(_ROOT, "_conf_schema.json")))
        for locale in ("zh-CN", "en-US"):
            payload = json.loads(read(os.path.join(_ROOT, ".astrbot-plugin", "i18n", f"{locale}.json")))
            unknown = set(payload.get("config", {})) - set(schema)
            self.assertFalse(unknown, f"{locale}.json 里存在 schema 没有的配置项：{unknown}")


class TestMetadata(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if yaml is None:
            raise unittest.SkipTest("需要 PyYAML")
        cls.metadata = yaml.safe_load(read(os.path.join(_ROOT, "metadata.yaml")))

    def test_required_fields_non_empty_strings(self):
        for field in REQUIRED_METADATA_FIELDS:
            self.assertIn(field, self.metadata, f"metadata.yaml 缺少必需字段 {field}")
            value = self.metadata[field]
            self.assertIsInstance(value, str, f"{field} 必须是字符串")
            self.assertTrue(value.strip(), f"{field} 不能为空")

    def test_metadata_size_limit(self):
        size = os.path.getsize(os.path.join(_ROOT, "metadata.yaml"))
        self.assertLessEqual(size, MAX_METADATA_BYTES, "metadata.yaml 不得超过 1MB")

    def test_name_follows_plugin_naming(self):
        name = self.metadata["name"]
        self.assertTrue(name.startswith("astrbot_plugin_"), "插件名建议以 astrbot_plugin_ 开头")
        self.assertEqual(name, name.lower(), "插件名必须全小写")
        self.assertNotIn(" ", name, "插件名不能含空格")
        self.assertNotIn("/", name, "插件名不能含路径分隔符")

    def test_version_is_semver_without_v_prefix(self):
        version = self.metadata["version"]
        self.assertFalse(version.lower().startswith("v"), "version 不要带 v 前缀（市场规范要求）")
        self.assertRegex(version, SEMVER_RE, "version 必须是语义化版本，如 0.1.0")
        if Version is not None:
            Version(version)  # 能被解析，避免 "0.1" 这种

    def test_repo_is_github_repo_url(self):
        repo = self.metadata.get("repo", "")
        self.assertRegex(repo, REPO_URL_RE, "repo 必须是 https://github.com/{owner}/{repo}")
        self.assertNotIn("/tree/", repo.rstrip("/"), "repo 不应指向子目录")

    def test_astrbot_version_is_valid_specifier(self):
        spec = self.metadata.get("astrbot_version")
        if spec is None:
            return
        self.assertIsInstance(spec, str)
        if SpecifierSet is not None:
            SpecifierSet(spec)  # 非法表达式会让插件无法加载

    def test_support_platforms_are_known_keys(self):
        known = {
            "aiocqhttp", "qq_official", "qq_official_webhook", "telegram", "wecom",
            "wecom_ai_bot", "lark", "dingtalk", "discord", "slack", "kook", "vocechat",
            "weixin_official_account", "weixin_oc", "satori", "misskey", "line", "matrix",
            "mattermost",
        }
        platforms = self.metadata.get("support_platforms") or []
        self.assertIsInstance(platforms, list)
        unknown = set(platforms) - known
        self.assertFalse(unknown, f"support_platforms 里有未知适配器名：{unknown}")

    def test_repo_matches_current_git_remote(self):
        """metadata.repo 必须和真实远端一致，否则市场安装会指到不存在的仓库。"""
        git_config = os.path.join(_ROOT, ".git", "config")
        if not os.path.isfile(git_config):
            self.skipTest("没有 .git/config（例如在 zip 包里跑测试）")
        config = read(git_config)
        match = re.search(r"url\s*=\s*(https://github\.com/[^\s]+)", config)
        if not match:
            self.skipTest("远端不是 https GitHub 地址")
        remote = match.group(1).removesuffix(".git").rstrip("/")
        declared = self.metadata["repo"].removesuffix(".git").rstrip("/")
        self.assertEqual(declared, remote, "metadata.yaml 的 repo 与 origin 不一致")

    def test_tags_are_lowercase_slugs(self):
        tags = self.metadata.get("tags") or []
        self.assertIsInstance(tags, list)
        for tag in tags:
            self.assertIsInstance(tag, str)
            self.assertEqual(tag, tag.strip().lower(), f"标签 {tag} 建议小写、无首尾空白")


class TestPublishSize(unittest.TestCase):
    def test_readme_relative_links_resolve(self):
        """README 里指向仓库内文件的相对链接必须存在（市场页会直接展示 README）。"""
        text = read(os.path.join(_ROOT, "README.md"))
        targets = re.findall(r"\]\(\./([^)#]+)\)", text)
        self.assertTrue(targets, "README 里没有相对链接？")
        missing = [t for t in targets if not os.path.exists(os.path.join(_ROOT, t))]
        self.assertFalse(missing, f"README 里有指向不存在文件的链接：{missing}")

    def test_readme_has_expected_sections(self):
        """README 是对外门面，关键章节不能丢。"""
        text = read(os.path.join(_ROOT, "README.md"))
        for keyword in ("安装", "命令", "配置", "兼容性", "许可证"):
            self.assertIn(keyword, text, f"README 缺少「{keyword}」相关内容")

    def test_changelog_version_matches_metadata(self):
        if yaml is None:
            self.skipTest("需要 PyYAML")
        metadata = yaml.safe_load(read(os.path.join(_ROOT, "metadata.yaml")))
        changelog = read(os.path.join(_ROOT, "CHANGELOG.md"))
        self.assertIn(
            f"## {metadata['version']}",
            changelog,
            "CHANGELOG 里应有当前版本号的条目",
        )

    def test_package_under_market_limit(self):
        total = 0
        for base, dirs, files in os.walk(_ROOT):
            dirs[:] = [d for d in dirs if d not in {".git", "__pycache__", ".venv", "node_modules"}]
            for name in files:
                if name.endswith((".pyc", ".pyo")):
                    continue
                total += os.path.getsize(os.path.join(base, name))
        self.assertLess(total, MAX_ZIP_BYTES, f"打包体积 {total} 字节超过 16MB 上限")

    def test_gitignore_keeps_runtime_data_out(self):
        text = read(os.path.join(_ROOT, ".gitignore"))
        for pattern in ("__pycache__", "account.json", ".venv"):
            self.assertIn(pattern, text, f".gitignore 应排除 {pattern}")

    def test_gitattributes_excludes_dev_files_from_archive(self):
        text = read(os.path.join(_ROOT, ".gitattributes"))
        for entry in ("/tests", "/tools"):
            self.assertIn(entry, text, f".gitattributes 应对 {entry} 设置 export-ignore")


if __name__ == "__main__":
    unittest.main(verbosity=2)
