#!/usr/bin/env python3
"""BlindSpatialAgent 交付打包。

为什么用 Python 而不是 `zip` 命令：
    1. **中文文件名**：Info-ZIP 不加 UTF-8 标志位，某些解压工具（尤其 Windows 自带）
       会把 `技术交付文档.md` 显示成乱码。`zipfile` 遇到非 ASCII 名会自动置 UTF-8 标志。
    2. **排除规则**：`zipfile` 可以在遍历时精确判断，不必拼一长串 -x 通配。
    3. **安全校验**：打包前后都能审计，命中 `.env` 直接失败，不留情面。

排除的东西（都不是源码，且部分含隐私）：
    .venv/                 虚拟环境（可由 requirements.txt 重建）
    ios/**/build*          Xcode 派生数据（本机实测 462MB）
    ios/_device_backups/   ★设备 App 容器备份 12MB，含 App 偏好/截图缓存，绝不能外发★
    **/xcuserdata/         Xcode 个人界面状态（与使用者绑定）
    __pycache__/ *.pyc     Python 字节码
    logs/                  运行日志与抓拍图（含用户真实画面）
    dist/                  打包输出自身
    .env                   ★真实 API Key★

用法：
    .venv/bin/python scripts/package.py
    .venv/bin/python scripts/package.py --out /tmp/x.zip

退出码：0 成功；1 安全检查未通过；2 参数/环境错误。
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 目录名（任意层级）命中即整体跳过
EXCLUDE_DIRS = {
    ".venv", "venv", "env",
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "build", "build-sim", "DerivedData",
    "_device_backups", "xcuserdata",
    "logs", "dist", ".git", ".idea",
}
# 文件名命中即跳过
EXCLUDE_FILES = {".env", ".DS_Store"}
# 后缀命中即跳过
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".log", ".bak")


def ensure_dir(p: Path) -> None:
    """幂等建目录。

    注意：本机沙箱对**已存在**的目录调用 `mkdir(exist_ok=True)` 也会抛 EEXIST，
    所以必须先判断再创建，不能依赖 exist_ok。
    """
    if not p.is_dir():
        p.mkdir(parents=True, exist_ok=True)


def read_version() -> str:
    """从配置中心读版本号，避免两处维护。"""
    cfg = PROJECT_ROOT / "config" / "config.yaml"
    try:
        m = re.search(r'^\s*version:\s*"([^"]+)"', cfg.read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else "0.0.0"
    except OSError:
        return "0.0.0"


def should_skip(path: Path) -> bool:
    rel = path.relative_to(PROJECT_ROOT)
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return True
    if path.name in EXCLUDE_FILES:
        return True
    if path.name.endswith(EXCLUDE_SUFFIXES):
        return True
    # 兜底：.env 的变体（.env.local / .env.prod …）
    if path.name == ".env" or path.name.startswith(".env."):
        # .env.example 是要发出去的模板，放行
        return path.name != ".env.example"
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="BlindSpatialAgent 交付打包")
    ap.add_argument("--out", default=None, help="输出 zip 路径（默认 dist/BlindSpatialAgent-v<版本>-<日期>.zip）")
    args = ap.parse_args()

    version = read_version()
    stamp = datetime.now().strftime("%Y%m%d")
    pkg_name = f"BlindSpatialAgent-v{version}"

    if args.out:
        out = Path(args.out).expanduser().resolve()
    else:
        ensure_dir(PROJECT_ROOT / "dist")
        out = PROJECT_ROOT / "dist" / f"{pkg_name}-{stamp}.zip"
    ensure_dir(out.parent)
    if out.exists():
        out.unlink()

    # --- 收集文件 ---
    files: list[Path] = []
    skipped: dict[str, int] = {}
    for p in sorted(PROJECT_ROOT.rglob("*")):
        if not p.is_file():
            continue
        if should_skip(p):
            # 归因到顶层目录名，便于打印"跳过了什么"
            rel = p.relative_to(PROJECT_ROOT)
            key = next((part for part in rel.parts if part in EXCLUDE_DIRS), rel.parts[0])
            skipped[key] = skipped.get(key, 0) + 1
            continue
        files.append(p)

    print("=== BlindSpatialAgent 交付打包 ===")
    print(f"项目    : {PROJECT_ROOT}")
    print(f"版本    : v{version}")
    print(f"输出    : {out}")
    print()

    # --- 写入 zip（zipfile 会自动为中文名置 UTF-8 标志）---
    # 只打开一次：循环内反复 append 会重复写中央目录，既慢又可能产生坏档。
    total_raw = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in files:
            rel = f.relative_to(PROJECT_ROOT)
            total_raw += f.stat().st_size
            # 顶层套一层带版本号的目录，解压不会散落
            zf.write(f, f"{pkg_name}/{rel.as_posix()}")

    print(f"已写入 {len(files)} 个文件，原始 {total_raw / 1048576:.2f} MB → 压缩后 {out.stat().st_size / 1048576:.2f} MB")
    if skipped:
        print()
        print("已跳过（按目录归因）：")
        for k, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>6} 个文件  ← {k}")

    # --- 安全校验 ---
    print()
    print("--- 安全检查 ---")
    problems: list[str] = []
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        hits_env = [n for n in names if Path(n).name == ".env" or Path(n).name.startswith(".env.") and not n.endswith(".env.example")]
        if hits_env:
            problems.append(f"产物含 .env 相关文件：{hits_env[:3]}")
        for forbidden in ("_device_backups/", "xcuserdata/", ".venv/", "__pycache__/", "build-sim/", "logs/"):
            if any(forbidden in n for n in names):
                problems.append(f"产物含禁用路径 {forbidden}")
        # 校验中文名可正确还原（UTF-8 标志是否生效）
        zh = [n for n in names if any("\u4e00" <= ch <= "\u9fff" for ch in n)]
        if zh:
            print(f"含中文名的条目 {len(zh)} 个，示例：{Path(zh[0]).name}")

    if problems:
        print("[失败] 安全检查未通过：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        out.unlink()
        print("已删除产物。", file=sys.stderr)
        return 1
    print("✓ 不含 .env / 设备备份 / 构建产物 / 缓存 / 日志")
    print()
    print("完成。分发前建议解压抽查一次。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
