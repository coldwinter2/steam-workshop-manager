#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""原样安装（copy）：把 staging/<modid>/ 移到 <安装根>/<modid>/。"""

import shutil

from .base import InstallContext, InstallError, InstallHandler, InstallResult


class CopyHandler(InstallHandler):
    type_key = "copy"
    label = "原样复制"

    def install(self, ctx: InstallContext) -> InstallResult:
        # copy 类呈现名 = modid/...，staging 下为 <staging>/<modid>/...
        src = ctx.staging_dir / ctx.modid
        if not src.is_dir():
            raise InstallError(f"未找到下载内容: {src}")
        dst = ctx.target_root / ctx.modid
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        ctx.target_root.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return InstallResult([f"{ctx.modid}/"], "原样安装")
