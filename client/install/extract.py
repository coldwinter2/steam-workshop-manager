#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""解压缩安装（extract）：服务端下行 <modid>.zip，客户端解压到 <安装根>/<modid>/。

如饥荒联机版：服务端把 Mod 目录打包成 zip 下行，
客户端解压成 mod 目录（zip 内是各文件的相对路径）。
"""

import zipfile
from pathlib import Path

from .base import InstallContext, InstallError, InstallHandler, InstallResult


def _safe_extract(zf: zipfile.ZipFile, dst: Path):
    """带 zip-slip 防护的解压。"""
    dst = dst.resolve()
    for member in zf.infolist():
        target = (dst / member.filename).resolve()
        if not str(target).startswith(str(dst)):
            raise InstallError(f"非法压缩条目(路径穿越): {member.filename}")
    zf.extractall(dst)


class ExtractHandler(InstallHandler):
    type_key = "extract"
    label = "解压缩"

    def install(self, ctx: InstallContext) -> InstallResult:
        # 呈现名 = modid.zip
        fname = None
        for f in ctx.files:
            name = Path(str(f)).name
            if name.lower().endswith(".zip") and name.startswith(ctx.modid):
                fname = name
                break
        if fname is None:
            cands = sorted(ctx.staging_dir.glob(f"{ctx.modid}.zip"))
            fname = cands[0].name if cands else None
        if fname is None:
            raise InstallError("未找到下载的 zip")

        src = ctx.staging_dir / fname
        if not src.is_file():
            raise InstallError(f"zip 缺失: {fname}")

        dst = ctx.target_root / ctx.modid
        if dst.exists():
            import shutil
            shutil.rmtree(dst, ignore_errors=True)
        dst.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(src) as zf:
                _safe_extract(zf, dst)
        except zipfile.BadZipFile as e:
            raise InstallError(f"zip 损坏: {e}") from e
        finally:
            src.unlink(missing_ok=True)
        return InstallResult([f"{ctx.modid}/"], "解压安装")
