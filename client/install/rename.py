#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""重命名安装（rename）：把下载到的单文件落到安装根，并按目标后缀改扩展名。

如 L4D2（appid 550）：源文件可能是 <任意名>.bin，客户端依据 install.params.target_ext
把它落地为 <modid>.vpk（客户端负责完成「改扩展名」这一步）。
"""

from pathlib import Path

from .base import InstallContext, InstallError, InstallHandler, InstallResult


class RenameHandler(InstallHandler):
    type_key = "rename"
    label = "重命名"

    @staticmethod
    def _norm_ext(ext: str) -> str:
        ext = str(ext).strip()
        return ext if ext.startswith(".") else "." + ext

    def install(self, ctx: InstallContext) -> InstallResult:
        # 1) 找到 staging 里已下载的源文件（呈现名以 modid 开头）
        staged = None
        for f in ctx.files:
            name = Path(str(f)).name
            if name.startswith(ctx.modid):
                staged = name
                break
        if staged is None:
            cands = sorted(ctx.staging_dir.glob(f"{ctx.modid}.*"))
            staged = cands[0].name if cands else None
        if staged is None:
            raise InstallError("未找到下载的单文件")

        src = ctx.staging_dir / staged
        if not src.is_file():
            raise InstallError(f"下载文件缺失: {staged}")

        # 2) 目标名 = modid + 目标后缀（客户端据此完成扩展名变更）
        target_ext = (ctx.params or {}).get("target_ext")
        if target_ext:
            out_name = f"{ctx.modid}{self._norm_ext(target_ext)}"
        else:
            out_name = staged  # 未配置目标后缀 -> 保持呈现名

        # 3) 落地到安装根
        dst = ctx.target_root / out_name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists() and dst.name != src.name:
            dst.unlink()
        src.replace(dst)
        return InstallResult([out_name], f"重命名安装为 {out_name}")
