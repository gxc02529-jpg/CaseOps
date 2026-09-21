# -*- coding: utf-8 -*-
"""依赖漏洞审计：扫描 pyproject.toml 解析出的**真实版本**，而不是区间下界。

## 为什么需要单独一个脚本

本仓库的依赖用区间约束（`>=x,<y`）。区间没有唯一解 —— 有安全意义的是
**pip 实际解析出来的那一个版本**。因此不能沿用"解析 requirements.txt 里的 `==` pin"
那套做法，必须先把解析结果取出来。

## 两个模式

    --emit-deps           从 pyproject.toml 抽出全部依赖声明（含各 extra），
                          逐行输出，供 `pip install --dry-run --report` 使用。

    --from-report FILE    读 pip 的 `--report` JSON，抽解析后的 name/version，
                          批量查 OSV，按包聚合输出 Markdown。

## 设计取舍

- **只用标准库**（tomllib / urllib / json）：CI 不为审计再装额外依赖。
- **先批量后明细**：`/v1/querybatch` 定位哪些包有漏洞，再对命中的漏洞 id
  逐个取明细；无漏洞的包零额外请求。
- **不自动断言"升到哪个版本"**：区间依赖的修复路径需要人工判断，
  报告只给出 OSV 声明的 fixed 版本集合，不做结论性推荐。
- **网络失败不致命但必须显式**：取不到数据时在 summary 里写明"审计未完成"，
  不静默假装通过。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/"
TIMEOUT = 45
ECOSYSTEM = "PyPI"

# 数据库自带严重度 → 排序权重
DB_SEVERITY_RANK = {
    "CRITICAL": 100.0, "HIGH": 80.0, "MODERATE": 60.0,
    "MEDIUM": 60.0, "LOW": 20.0,
}
NET_ERRORS = (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError)


# ----------------------------------------------------------------- helpers

def _post(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "dependency-audit"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "dependency-audit"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def emit_deps(pyproject: str) -> int:
    """把 pyproject.toml 里声明的依赖逐行打到 stdout。

    覆盖 `project.dependencies` 与全部 `project.optional-dependencies`，
    使审计范围与 CI 实际会安装的集合一致。
    """
    if sys.version_info < (3, 11):
        print("需要 Python 3.11+ 才能读取 pyproject.toml（tomllib）", file=sys.stderr)
        return 2
    import tomllib

    with open(pyproject, "rb") as fh:
        data = tomllib.load(fh)
    project = data.get("project", {})

    specs: list[str] = list(project.get("dependencies", []) or [])
    for group in (project.get("optional-dependencies") or {}).values():
        specs.extend(group or [])

    seen: set[str] = set()
    for spec in specs:
        spec = spec.strip()
        if not spec or spec in seen:
            continue
        seen.add(spec)
        print(spec)
    return 0


def load_resolved(report_path: str) -> list[tuple[str, str]]:
    """从 pip --report JSON 里抽 (name, version)。"""
    with open(report_path, encoding="utf-8") as fh:
        report = json.load(fh)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in report.get("install", []) or []:
        meta = item.get("metadata") or {}
        name, ver = meta.get("name"), meta.get("version")
        if not name or not ver:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((name, ver))
    out.sort(key=lambda p: p[0].lower())
    return out


def severity_of(vuln: dict) -> tuple[str, float]:
    """返回 (可读严重度, 排序权重)。

    优先用数据库自带严重度（GHSA 记录带 `database_specific.severity`），
    缺失时退回 CVSS 分数。
    """
    db = (vuln.get("database_specific") or {}).get("severity")
    if isinstance(db, str) and db.strip():
        up = db.strip().upper()
        return up, DB_SEVERITY_RANK.get(up, 10.0)

    best: float | None = None
    for entry in vuln.get("severity") or []:
        score = entry.get("score")
        if not score or not str(score).startswith("CVSS"):
            continue
        try:
            value = float(str(score).rstrip("/").split("/")[-1])
        except ValueError:
            continue
        best = value if best is None else max(best, value)
    if best is not None:
        return f"CVSS {best:.1f}", best * 10.0
    return "未标注", 0.0


def fixed_versions(vuln: dict, package: str) -> list[str]:
    """收集 OSV 为该包声明的 `fixed` 事件版本。

    只取版本型 range（SEMVER / ECOSYSTEM）——`GIT` 类型的 range 里 `fixed`
    是 commit SHA，混进版本列表会让输出失去可操作性。
    """
    wanted = package.lower()
    found: set[str] = set()
    for aff in vuln.get("affected") or []:
        pkg = aff.get("package") or {}
        if (pkg.get("name") or "").lower() != wanted:
            continue
        for rng in aff.get("ranges") or []:
            if (rng.get("type") or "").upper() == "GIT":
                continue
            for event in rng.get("events") or []:
                if "fixed" in event:
                    found.add(str(event["fixed"]))
    return sorted(found)


def version_key(ver: str) -> tuple:
    """把版本串拆成可比较的键。

    不引入 packaging 依赖（审计脚本要零第三方依赖）：
    数字段按数值比较，字母段按字典序，段数不同时长的排后。
    """
    parts = re.findall(r"\d+|[A-Za-z]+", ver)
    key = []
    for part in parts:
        key.append((0, int(part), "") if part.isdigit() else (1, 0, part.lower()))
    return tuple(key)


def highest_fixed(targets: list[str]) -> str | None:
    """取各漏洞声明修复版本中的最大值。

    语义：升到该版本即可覆盖 OSV 为这个包声明的**全部**已知漏洞。
    比较不了（例如非标准版本号）时返回 None，由调用方降级展示。
    """
    usable = [t for t in targets if re.match(r"^[0-9]", t)]
    if not usable:
        return None
    return max(usable, key=version_key)


def audit(pairs: list[tuple[str, str]], out) -> int:
    if not pairs:
        print("依赖解析结果为空，未执行审计。", file=out)
        return 0

    queries = [{"package": {"name": n, "ecosystem": ECOSYSTEM}, "version": v}
               for n, v in pairs]
    try:
        batch = _post(OSV_BATCH, {"queries": queries})
    except NET_ERRORS as exc:
        print(f"OSV 批量查询失败：{exc}", file=out)
        print("", file=out)
        print("**审计未完成，请勿据此认为依赖无漏洞。**", file=out)
        return 1

    results = batch.get("results") or []
    hits: dict[str, list[str]] = {}
    for (name, _ver), res in zip(pairs, results):
        ids = [v.get("id") for v in (res.get("vulns") or []) if v.get("id")]
        if ids:
            hits[name] = ids

    print(f"- 依赖解析结果：**{len(pairs)}** 个 distribution", file=out)
    print(f"- 命中已知漏洞的包：**{len(hits)}** 个", file=out)
    print("", file=out)

    if not hits:
        print("未发现已知漏洞。数据源：OSV（聚合 GitHub Advisory / PyPA / NVD）。", file=out)
        return 0

    version_of = dict(pairs)
    cache: dict[str, dict] = {}
    rows: list[tuple[str, str, int, str, float, list[str]]] = []

    for name, ids in sorted(hits.items()):
        best_label, best_rank = "未标注", -1.0
        targets: set[str] = set()
        for vid in ids:
            if vid not in cache:
                try:
                    cache[vid] = _get(OSV_VULN + vid)
                except NET_ERRORS:
                    cache[vid] = {}
            vuln = cache[vid]
            label, rank = severity_of(vuln)
            if rank > best_rank:
                best_label, best_rank = label, rank
            targets |= set(fixed_versions(vuln, name))
        rows.append((name, version_of[name], len(ids), best_label,
                     best_rank, sorted(targets), highest_fixed(sorted(targets))))

    rows.sort(key=lambda r: (-r[4], r[0].lower()))

    print("| 包 | 解析版本 | 漏洞数 | 最高严重度 | 可一次清空该包全部告警的版本 |", file=out)
    print("| --- | --- | --- | --- | --- |", file=out)
    for name, ver, count, sev, _rank, targets, floor in rows:
        if floor:
            cell = f"`>= {floor}`"
        elif targets:
            cell = "、".join(f"`{t}`" for t in targets[:3])
        else:
            cell = "未声明"
        print(f"| `{name}` | {ver} | {count} | {sev} | {cell} |", file=out)
    print("", file=out)
    print("> 目标版本取 OSV 为各漏洞声明的 `fixed` 事件的最大值（GIT 类型的 range 已排除）。"
          "区间依赖能否直接升到该版本需人工判断（可能跨越主版本）——"
          "本表只做定位，不代替迁移评审。", file=out)
    return 0


# -------------------------------------------------------------------- main

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="依赖漏洞审计（按 pip 解析结果）")
    ap.add_argument("--emit-deps", action="store_true",
                    help="输出 pyproject.toml 声明的依赖清单，供 pip 解析")
    ap.add_argument("--from-report", metavar="FILE",
                    help="读 pip --report JSON 并审计解析版本")
    ap.add_argument("--pyproject", default="pyproject.toml")
    args = ap.parse_args(argv[1:])

    if args.emit_deps:
        return emit_deps(args.pyproject)

    if args.from_report:
        if not os.path.exists(args.from_report):
            print(f"找不到 report 文件：{args.from_report}", file=sys.stderr)
            return 2
        pairs = load_resolved(args.from_report)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write("## 依赖漏洞审计（按 pip 解析结果）\n\n")
                rc = audit(pairs, fh)
            audit(pairs, sys.stdout)
            return rc
        return audit(pairs, sys.stdout)

    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
