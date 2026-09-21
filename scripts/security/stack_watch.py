# -*- coding: utf-8 -*-
"""每日依赖风险日报：把「声明下界」和「版本漂移」做成可逐日对比的工程记录。

## 判定口径（为什么是"下界"）

本仓库依赖用区间约束（`>=x,<y`）。区间没有唯一解，pip 解到哪一版取决于
解析当天各包的发布状态。**最坏情况就是下界** —— 若 `>=x` 里的 x 本身
仍落在某个 advisory 的受影响区间内，那么"暂时没中招"只是运气。

所以日报只回答一个问题：**按声明下界算，今天有哪些包仍在受影响区间内，
以及升到哪一版能一次清空该包全部告警。**

## 两个数据源（保证每天都有真实内容变化）

1. **OSV 告警** —— 新 advisory 持续发布，结论会变。
2. **版本漂移** —— PyPI 最新版持续推进，`<` 上界会逐渐变成"挡路"。

两者都与日期强相关，因此报告不是"改个时间戳"，而是每天重新计算得出的结论。

## 与 `dependency_audit.py` 的分工

| 脚本 | 输入 | 用途 |
|---|---|---|
| `dependency_audit.py` | pip `--report`（**已解析**的真实版本） | CI 门禁：拦"这次装出来的版本有洞" |
| `stack_watch.py`（本脚本） | 清单里的**声明约束**（下界） | 每日巡检：拦"声明的下界本身就不干净" |

前者要跑完整 pip 解析（数分钟），后者只查 OSV/PyPI（几十秒）—— 所以日报用后者。

## 设计取舍

- **仅标准库**：`tomllib` / `urllib` / `json` / `re`，CI 不为它装依赖。
- **版本比较不引入 `packaging`**：用元组化比较，能正确处理 `1.10 > 1.9`；
  但**预发布版本排序与 PEP 440 不一致**（`1.0a1` 会被排到 `1.0` 之后），
  日报对预发布不敏感，可接受。
- **网络失败必须显式**：取不到 OSV 就写明"本次未完成"，不静默输出"无漏洞"。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

OSV_BATCH = "https://api.osv.dev/v1/querybatch"
OSV_VULN = "https://api.osv.dev/v1/vulns/"
ECOSYSTEM = "PyPI"
TIMEOUT = 45
HISTORY_LIMIT = 90
NET_ERRORS = (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError)

SEVERITY_RANK = {"CRITICAL": 100.0, "HIGH": 80.0, "MODERATE": 60.0, "MEDIUM": 60.0, "LOW": 20.0}
SPEC_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(.*)$")


# --------------------------------------------------------------------- 版本

def vkey(ver: str) -> tuple:
    """把版本串拆成可比较的元组（数字段按数值，字母段按字典序）。"""
    out = []
    for part in re.findall(r"\d+|[A-Za-z]+", str(ver)):
        out.append((0, int(part), "") if part.isdigit() else (1, 0, part.lower()))
    return tuple(out)


def vlt(a: str, b: str) -> bool:
    return vkey(a) < vkey(b)


def high_fixed(versions: list[str]) -> str | None:
    usable = [v for v in versions if re.match(r"^[0-9]", v)]
    return max(usable, key=vkey) if usable else None


# ------------------------------------------------------------------- 清单解析

def parse_requirement(spec: str) -> tuple[str, str, str | None] | None:
    """把一条依赖声明拆成 (包名, 约束串, 下界版本)。

    覆盖 `name[extra]>=1.2,<2` / `name==1.2.3` / `name~=1.2` / 裸 `name`。
    下界取约束里的最小允许版本；无约束时返回 None。
    """
    spec = spec.strip()
    if not spec or spec.startswith("#") or spec.startswith("-"):
        return None
    m = SPEC_RE.match(spec.split(";")[0].strip())
    if not m:
        return None
    name, _extras, cons = m.group(1), m.group(2), (m.group(3) or "").strip()

    floor = None
    for op, ver in re.findall(r"(===|==|~=|>=|<=|!=|>|<)\s*([0-9][^\s,;]*)", cons):
        if op in ("==", "===", "~=", ">="):
            floor = ver if floor is None or vlt(ver, floor) else floor
        elif op == "<" and floor is None:
            floor = None
    return name, cons, floor


def load_manifest(path: str) -> tuple[str, list[str]]:
    """返回 (清单种类, 原始声明列表)。"""
    if path.endswith(".toml"):
        if sys.version_info < (3, 11):
            raise RuntimeError("读取 pyproject.toml 需要 Python 3.11+（tomllib）")
        import tomllib
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
        project = data.get("project", {}) or {}
        specs = list(project.get("dependencies") or [])
        for group in (project.get("optional-dependencies") or {}).values():
            specs.extend(group or [])
        extras = len(project.get("optional-dependencies") or {})
        return f"pyproject.toml（含 {extras} 组 extras）", specs
    with open(path, encoding="utf-8") as fh:
        return os.path.basename(path), [ln for ln in fh if ln.strip()]


def detect_manifest(root: str) -> str | None:
    for cand in ("pyproject.toml", "requirements.txt", "requirements-dev.txt"):
        p = os.path.join(root, cand)
        if os.path.exists(p):
            return p
    return None


# --------------------------------------------------------------------- OSV

def _post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "stack-watch"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read().decode())


def _get_json(url: str, timeout: int = TIMEOUT) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "stack-watch"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fixed_versions(vuln: dict, package: str) -> set[str]:
    """收集该 advisory 为该包声明的 `fixed` 版本。

    GIT 类型 range 里的 `fixed` 是 commit SHA，混进版本列表会失去可操作性，故排除。
    """
    wanted = package.lower()
    out: set[str] = set()
    for aff in vuln.get("affected") or []:
        pkg = aff.get("package") or {}
        if (pkg.get("name") or "").lower() != wanted:
            continue
        for rng in aff.get("ranges") or []:
            if (rng.get("type") or "").upper() == "GIT":
                continue
            for ev in rng.get("events") or []:
                if "fixed" in ev:
                    out.add(str(ev["fixed"]))
    return out


def severity_of(vuln: dict) -> tuple[str, float]:
    db = (vuln.get("database_specific") or {}).get("severity")
    if isinstance(db, str) and db.strip():
        up = db.strip().upper()
        return up, SEVERITY_RANK.get(up, 10.0)
    best = None
    for entry in vuln.get("severity") or []:
        s = entry.get("score")
        if not s or not str(s).startswith("CVSS"):
            continue
        try:
            val = float(str(s).rstrip("/").split("/")[-1])
        except ValueError:
            continue
        best = val if best is None else max(best, val)
    return (f"CVSS {best:.1f}", best * 10.0) if best is not None else ("未标注", 0.0)


def pypi_latest(name: str) -> str | None:
    try:
        d = _get_json(f"https://pypi.org/pypi/{name}/json", timeout=20)
        return (d.get("info") or {}).get("version")
    except NET_ERRORS:
        return None


# -------------------------------------------------------------------- 主流程

def build(repo: str, manifest_path: str, out_dir: str) -> int:
    kind, raw = load_manifest(manifest_path)
    deps: list[tuple[str, str, str | None]] = []
    seen: set[str] = set()
    for item in raw:
        parsed = parse_requirement(item)
        if not parsed:
            continue
        name, cons, floor = parsed
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        deps.append((name, cons, floor))
    deps.sort(key=lambda d: d[0].lower())

    generated = datetime.now(timezone.utc)
    ok = True

    # 1) 查 OSV —— **带上声明下界作为 version**
    #    这样 OSV 直接只回「该版本受影响」的 advisory，等价于
    #    「下界 ∩ 受影响区间」求交，但省掉了大量无用的明细请求。
    #    （实测：不带版本再自己求交，torch/transformers 这类包会拉几十次明细，
    #      本地跑一次要数分钟；带版本后明细只拉真命中的。）
    findings: list[dict] = []
    unknown: list[dict] = []
    queries = []
    for name, _cons, floor in deps:
        q: dict = {"package": {"name": name, "ecosystem": ECOSYSTEM}}
        if floor:
            q["version"] = floor
        queries.append(q)

    try:
        batch = _post(OSV_BATCH, {"queries": queries})
        results = batch.get("results") or []
    except NET_ERRORS as exc:
        print(f"  OSV 批量查询失败：{exc}", file=sys.stderr)
        results = []
        ok = False

    vuln_cache: dict[str, dict] = {}
    detail_fail = 0
    for (name, cons, floor), res in zip(deps, results):
        ids = [v.get("id") for v in (res.get("vulns") or []) if v.get("id")]
        if not ids:
            continue
        if floor is None:
            # 未声明版本 → 无法判定是否命中，单列一档，不混进结论
            unknown.append({"package": name, "count": len(ids)})
            continue
        hits, best_label, best_rank, fix_targets = [], "未标注", -1.0, set()
        for vid in ids:
            if vid not in vuln_cache:
                try:
                    vuln_cache[vid] = _get_json(OSV_VULN + vid)
                except NET_ERRORS:
                    vuln_cache[vid] = {}
                    detail_fail += 1
            vuln = vuln_cache[vid]
            label, rank = severity_of(vuln)
            if rank > best_rank:
                best_label, best_rank = label, rank
            fix_targets |= fixed_versions(vuln, name)
            hits.append({
                "id": vid,
                "aliases": [a for a in (vuln.get("aliases") or []) if a.startswith("CVE")],
                "summary": (vuln.get("summary") or "").strip(),
            })
        if hits:
            findings.append({
                "package": name, "spec": cons or "（未声明）", "floor": floor,
                "count": len(hits), "severity": best_label, "rank": best_rank,
                "clear_version": high_fixed(sorted(fix_targets)),
                "advisories": sorted(hits, key=lambda h: h["id"]),
            })
    if detail_fail:
        print(f"  警告：{detail_fail} 次 advisory 明细取回失败，严重度可能降级为「未标注」",
              file=sys.stderr)
    findings.sort(key=lambda f: (-f["rank"], f["package"].lower()))

    # 2) 查版本漂移：PyPI 最新版是否已被声明上界挡住
    drift = []
    for name, cons, floor in deps:
        up = re.search(r"<\s*([0-9][^\s,;]*)", cons or "")
        if not up or floor is None:
            continue
        latest = pypi_latest(name)
        if not latest:
            continue
        if not vlt(latest, up.group(1)):
            drift.append({"package": name, "floor": floor, "upper": up.group(1),
                          "latest": latest})

    # 3) 读历史 + 算 delta
    os.makedirs(out_dir, exist_ok=True)
    index_path = os.path.join(out_dir, "index.json")
    prev = {}
    if os.path.exists(index_path):
        try:
            with open(index_path, encoding="utf-8") as fh:
                prev = json.load(fh)
        except (json.JSONDecodeError, OSError):
            prev = {}

    today = generated.strftime("%Y-%m-%d")
    cur_ids = {f"{f['package']}::{a['id']}" for f in findings for a in f["advisories"]}
    prev_ids = set()
    prev_entry = None
    for h in prev.get("history") or []:
        if h.get("date") == today:
            continue
        prev_entry = h
        prev_ids = set(h.get("finding_ids") or [])
        break
    new_ids = sorted(cur_ids - prev_ids) if prev_ids else []
    gone_ids = sorted(prev_ids - cur_ids) if prev_ids else []

    history = [h for h in (prev.get("history") or []) if h.get("date") != today]
    history.append({
        "date": today,
        "packages": len(deps),
        "affected": len(findings),
        "high": sum(1 for f in findings if f["rank"] >= 80),
        "drift": len(drift),
        "finding_ids": sorted(cur_ids),
        "audit_ok": ok,
    })
    history = history[-HISTORY_LIMIT:]

    index = {
        "repo": repo, "manifest": kind, "manifest_path": os.path.basename(manifest_path),
        "date": today, "generated_at": generated.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "packages_total": len(deps), "audit_ok": ok,
        "findings": findings, "unknown": unknown, "drift": drift, "history": history,
        "new_findings": new_ids, "cleared_findings": gone_ids,
    }
    with open(index_path, "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, indent=1)

    # 4) 生成 Markdown
    report_path = os.path.join(out_dir, f"{today}.md")
    with open(report_path, "w", encoding="utf-8") as o:
        w = o.write
        w(f"# 依赖风险日报 · {repo}\n\n")
        w("> 由 `.github/workflows/stack-watch.yml` 每日自动生成，机器产出，请勿手工编辑。\n\n")
        w("| 项目 | 值 |\n| --- | --- |\n")
        w(f"| 生成时间（UTC） | {generated.strftime('%Y-%m-%d %H:%M')} |\n")
        w(f"| 依赖清单 | {kind} |\n")
        w(f"| 声明依赖 | {len(deps)} 个包 |\n")
        w(f"| 上一次巡检 | {prev_entry['date'] if prev_entry else '（首次运行）'} |\n")
        w("| 数据来源 | OSV（聚合 GitHub Advisory / PyPA / NVD）、PyPI |\n\n")
        if not ok:
            w("> **本次 OSV 查询未完成，报告不完整 —— 请勿据此认为依赖无漏洞。**\n\n")

        w("## 1. 判定口径\n\n")
        w("按各包**声明下界**（约束里允许的最低版本，即 pip 可能解析到的最坏情况）"
          "与 OSV 受影响区间求交。下界命中 = 即便解析到最低允许版本，仍在受影响范围内。\n\n")

        w("## 2. 概览\n\n")
        w("| 指标 | 今日 | 昨日 | 变化 |\n| --- | --- | --- | --- |\n")

        def row(label, cur, prv):
            if prv is None:
                return w(f"| {label} | {cur} | — | — |\n")
            d = cur - prv
            arrow = "—" if d == 0 else (f"+{d}" if d > 0 else str(d))
            return w(f"| {label} | {cur} | {prv} | {arrow} |\n")

        row("声明依赖数", len(deps), prev_entry["packages"] if prev_entry else None)
        row("下界仍受影响的包", len(findings), prev_entry["affected"] if prev_entry else None)
        row("其中 HIGH 及以上",
            sum(1 for f in findings if f["rank"] >= 80),
            prev_entry["high"] if prev_entry else None)
        row("上界已挡路的包", len(drift), prev_entry["drift"] if prev_entry else None)
        w("\n")

        w("## 3. 下界仍命中已知漏洞的包\n\n")
        if not findings:
            w("未发现。\n")
            if not ok:
                w("\n（注意：本次查询未完成，此结论不可信。）\n")
        else:
            w("| 包 | 声明约束 | 下界 | 漏洞数 | 最高严重度 | 清空该包全部告警所需版本 |\n")
            w("| --- | --- | --- | --- | --- | --- |\n")
            for f in findings:
                clear = f"`>= {f['clear_version']}`" if f["clear_version"] else "未声明"
                w(f"| `{f['package']}` | `{f['spec']}` | {f['floor']} | {f['count']} | "
                  f"{f['severity']} | {clear} |\n")
            w("\n> 目标版本取 OSV 为各漏洞声明的 `fixed` 事件的最大值（GIT 类型 range 已排除）。"
              "区间依赖能否直接升到该版本需人工判断，本表只做定位。\n")

            if new_ids:
                w("\n### 3.1 较上次巡检新增\n\n")
                for fid in new_ids:
                    pkg, vid = fid.split("::", 1)
                    w(f"- `{pkg}` — {vid}\n")
            if gone_ids:
                w("\n### 3.2 较上次巡检已消除\n\n")
                for fid in gone_ids:
                    pkg, vid = fid.split("::", 1)
                    w(f"- `{pkg}` — {vid}\n")
        w("\n")

        w("## 4. 未声明版本的依赖\n\n")
        if not unknown:
            w("无。全部声明依赖都带版本约束，可判定。\n")
        else:
            w("以下依赖未声明版本约束，无法判定是否命中已知漏洞"
              "（建议补上下界，否则该包处于「不可判定」状态）：\n\n")
            w("| 包 | 该包已知 advisory 数 |\n| --- | --- |\n")
            for u in unknown:
                w(f"| `{u['package']}` | {u['count']} |\n")
        w("\n")

        w("## 5. 版本漂移\n\n")
        if not drift:
            w("无。所有 `>` 上界均未挡住最新发布。\n")
        else:
            w("以下包的最新发布已被声明的上界排除，说明版本范围值得重新评估：\n\n")
            w("| 包 | 下界 | 声明上界 | PyPI 最新 |\n| --- | --- | --- | --- |\n")
            for d in drift:
                w(f"| `{d['package']}` | {d['floor']} | `< {d['upper']}` | {d['latest']} |\n")
        w("\n")

        w("## 6. 历史\n\n")
        w("| 日期 | 依赖数 | 受影响 | HIGH+ | 漂移 | 报告 |\n| --- | --- | --- | --- | --- | --- |\n")
        for h in reversed(history[-14:]):
            mark = "本次" if h["date"] == today else f"[{h['date']}](./{h['date']}.md)"
            w(f"| {h['date']} | {h['packages']} | {h['affected']} | {h['high']} | "
              f"{h['drift']} | {mark} |\n")
        w(f"\n> 报告保留最近 {HISTORY_LIMIT} 天，机器可读历史见 [`index.json`](./index.json)。\n")

    print(f"  清单 {manifest_path}（{kind}）：{len(deps)} 个包")
    print(f"  下界仍受影响：{len(findings)} 个   新增 {len(new_ids)} / 消除 {len(gone_ids)}")
    print(f"  版本漂移：{len(drift)} 个")
    print(f"  产出：{report_path}")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="生成每日依赖风险日报")
    ap.add_argument("--repo-name", required=True)
    ap.add_argument("--out-dir", default="reports/stack-watch")
    ap.add_argument("--manifest")
    ap.add_argument("--root", default=".")
    args = ap.parse_args(argv[1:])

    manifest = args.manifest or detect_manifest(args.root)
    if not manifest:
        print("找不到 pyproject.toml 或 requirements.txt", file=sys.stderr)
        return 2
    return build(args.repo_name, manifest, args.out_dir)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
