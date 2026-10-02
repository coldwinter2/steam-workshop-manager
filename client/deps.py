#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
依赖图计算（客户端）

- build_graph：把服务端返回的 Mod 列表构造成 itemid -> 直接依赖列表 的图
- closure：从一组根节点出发的传递依赖闭包（Mod 自身 + 全部依赖）
- plan_download：下载计划 = 所选 Mod + 其传递依赖
- plan_delete：删除计划 = 所选 Mod + 其孤儿依赖；
               共享依赖（仍被其它「已下载」Mod 需要）保留，不删

全部为纯函数，便于测试。
"""


def build_graph(mods: list) -> dict:
    """服务端 mods 列表 -> {itemid: [直接依赖 itemid, ...]}。

    依赖项若不在列表中，也会作为键出现（值为空列表），
    保证闭包计算不会漏掉外部依赖节点。
    """
    graph: dict[str, list] = {}
    for m in mods or []:
        iid = str(m.get("itemid", ""))
        if not iid:
            continue
        deps = [str(d) for d in (m.get("deps") or []) if str(d)]
        graph.setdefault(iid, [])
        # 合并去重（同一 itemid 可能重复出现）
        for d in deps:
            if d not in graph[iid]:
                graph[iid].append(d)
        # 保证依赖节点也有键
        for d in deps:
            graph.setdefault(d, [])
    return graph


def closure(graph: dict, roots) -> set:
    """返回 roots 及其全部传递依赖构成的集合（含 roots 自身）。"""
    seen: set = set()
    stack = [str(r) for r in roots]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for d in graph.get(cur, ()):
            if d not in seen:
                stack.append(d)
    return seen


def plan_download(graph: dict, selected) -> list:
    """下载计划：所选 Mod + 其传递依赖。

    返回 BFS 顺序列表（所选 Mod 在前，其依赖随后），便于「逐个下载」。
    """
    selected = [str(s) for s in selected]
    ordered: list = []
    seen: set = set()
    frontier = list(selected)
    while frontier:
        nxt: list = []
        for cur in frontier:
            if cur in seen:
                continue
            seen.add(cur)
            ordered.append(cur)
            for d in graph.get(cur, ()):
                if d not in seen:
                    nxt.append(d)
        frontier = nxt
    return ordered


def plan_delete(graph: dict, selected, downloaded, manual) -> tuple[set, set]:
    """删除计划。

    参数：
      graph:      itemid -> 直接依赖列表
      selected:   用户勾选要删除的 Mod
      downloaded: 当前本地已下载的 Mod 集合
      manual:     来源为「手动添加」的 Mod 集合（真正的顶层根）

    返回 (to_delete, kept_shared)：
      to_delete   实际要删除的集合 = 所选 Mod（恒删）
                  + 其传递依赖中「不再被任何剩余手动 Mod 需要」的孤儿依赖
      kept_shared 被保留的共享依赖（仍被其它剩余已下载 Mod 依赖），不删

    关键：keep 的根只能是「手动」且「已下载」且「未被勾选删除」的 Mod。
    自动引入（auto）依赖不能当根——否则它会「自我保护」，
    导致本应级联删除的孤儿依赖被错误保留。
    """
    selected = {str(s) for s in selected}
    downloaded = {str(d) for d in downloaded}
    manual = {str(m) for m in manual}

    # 所选 Mod 的传递闭包（自身 + 依赖）
    sel_closure = closure(graph, selected)

    # 删除后仍然存活的根：手动 + 已下载 + 未被勾选
    keep_roots = (manual & downloaded) - selected
    keep = closure(graph, keep_roots)

    to_delete: set = set(selected)
    kept_shared: set = set()
    for m in sel_closure:
        if m in selected:
            continue
        if m in keep:
            kept_shared.add(m)   # 共享依赖，仍被剩余手动 Mod 需要 -> 保留
        else:
            to_delete.add(m)     # 孤儿依赖 -> 删除

    return to_delete, kept_shared
