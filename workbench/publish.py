#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把工作台前端与构建数据组装成可直接部署的静态目录

用法：
    python3 workbench/publish.py            # 只组装 dist/
    python3 workbench/publish.py --stage    # 同时组装 .zcode-site/stage/
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "dist"
STAGE = ROOT / ".zcode-site" / "stage"
SERVER_TEMPLATE = ROOT / "static-server.mjs"

START_SH = """#!/usr/bin/env bash
# ETF 买点工作台：静态站点启动脚本
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

SITE_DIR=dist
export SPA_FALLBACK=1

export PORT="${PORT:-3000}"
exec node static-server.mjs "$SITE_DIR"
"""


def assemble_dist() -> None:
    SITE.mkdir(parents=True, exist_ok=True)
    for item in (ROOT / "static").iterdir():
        if item.is_file():
            shutil.copy2(item, SITE / item.name)
    if not (SITE / "data" / "base.json").exists():
        raise SystemExit("缺少 dist/data/base.json，请先运行 python3 workbench/build_data.py")
    print(f"dist 就绪：{SITE}")


def assemble_stage() -> None:
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)
    shutil.copytree(SITE, STAGE / "dist")
    shutil.copy2(SERVER_TEMPLATE, STAGE / "static-server.mjs")
    start = STAGE / "start.sh"
    with start.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(START_SH)
    start.chmod(0o755)
    total = sum(f.stat().st_size for f in STAGE.rglob("*") if f.is_file())
    print(f"stage 就绪：{STAGE}（{total / 1024 / 1024:.1f}MB）")


def main() -> None:
    ap = argparse.ArgumentParser(description="组装 ETF 买点工作台静态站点")
    ap.add_argument("--stage", action="store_true", help="同时组装 .zcode-site/stage")
    args = ap.parse_args()
    assemble_dist()
    if args.stage:
        assemble_stage()


if __name__ == "__main__":
    main()
