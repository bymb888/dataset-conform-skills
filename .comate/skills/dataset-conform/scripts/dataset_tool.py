#!/usr/bin/env python3
"""dataset_tool.py — 通用结构化数据集工具：体检 / 分片 / 合并 / 对比。

设计前提：格式与结构问题由本脚本确定性判定，语义问题（释义、翻译、逻辑推导）
留给子代理判断。spec.json 里的 semanticRules 只被透传，不做机械校验。

子命令：
  audit  按 spec 全量校验，输出 issues 与摘要
  split  按条目分片，供并行子代理修复
  merge  合并修复分片，附完整性守卫与变更统计
  diff   对比两份数据集，输出字段级变更摘要

仅依赖标准库。用法示例见 skill 的 SKILL.md。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# --------------------------------------------------------------------------
# 数据读写
# --------------------------------------------------------------------------


def detect_newline(path: Path) -> str:
    raw = path.read_bytes()
    return "\r\n" if b"\r\n" in raw else "\n"


def load_entries(path: Path, container: str) -> tuple[list[Any], list[str] | None]:
    """返回 (entries, keys)。keys 仅 object_map 容器有值。"""
    text = path.read_text(encoding="utf-8")
    if container == "jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()], None
    data = json.loads(text)
    if container == "object_map":
        if not isinstance(data, dict):
            raise SystemExit(f"[错误] {path} 顶层不是对象，与 container=object_map 不符")
        keys = list(data.keys())
        return [data[k] for k in keys], keys
    if not isinstance(data, list):
        raise SystemExit(f"[错误] {path} 顶层不是数组，请用 --container object_map 或 jsonl")
    return data, None


def dump_entries(
    path: Path,
    entries: list[Any],
    keys: list[str] | None,
    container: str,
    newline: str = "\n",
    indent: int = 2,
) -> None:
    if container == "jsonl":
        body = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n"
    else:
        payload: Any = dict(zip(keys, entries)) if container == "object_map" and keys else entries
        body = json.dumps(payload, ensure_ascii=False, indent=indent) + "\n"
    path.write_bytes(body.replace("\n", newline).encode("utf-8"))


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# 路径解析：支持 "a.b"、"a[].b"、"a[].b[].c"
# --------------------------------------------------------------------------


def tokenize(path: str) -> list[str]:
    tokens: list[str] = []
    for segment in path.split("."):
        name, depth = segment, 0
        while name.endswith("[]"):
            name, depth = name[:-2], depth + 1
        if name:
            tokens.append(name)
        tokens.extend(["[]"] * depth)
    return tokens


def resolve(obj: Any, path: str) -> list[tuple[str, Any]]:
    """解析路径，返回 [(具体路径, 值)]；不存在的分支直接跳过。"""
    results: list[tuple[str, Any]] = []

    def walk(cur: Any, tokens: list[str], trail: str) -> None:
        if not tokens:
            results.append((trail, cur))
            return
        head, rest = tokens[0], tokens[1:]
        if head == "[]":
            if isinstance(cur, list):
                for i, item in enumerate(cur):
                    walk(item, rest, f"{trail}[{i}]")
        elif isinstance(cur, dict) and head in cur:
            walk(cur[head], rest, f"{trail}.{head}" if trail else head)

    walk(obj, tokenize(path), "")
    return results


# --------------------------------------------------------------------------
# 结构校验
# --------------------------------------------------------------------------

TYPE_NAMES = {dict: "object", list: "array", str: "string", bool: "boolean", int: "integer", float: "number"}


def type_name(value: Any) -> str:
    if value is None:
        return "null"
    return TYPE_NAMES.get(type(value), type(value).__name__)


class Issue(dict):
    pass


def mk(path: str, rule: str, message: str, severity: str = "error") -> Issue:
    return Issue(path=path, rule=rule, severity=severity, message=message)


def check_value(value: Any, node: dict, path: str, issues: list[Issue], forbid_extra: bool) -> None:
    expected = node.get("type", "any")
    if value is None:
        if not node.get("nullable"):
            issues.append(mk(path, "null_value", "值为 null，参考格式不允许"))
        return

    if expected == "object":
        if not isinstance(value, dict):
            issues.append(mk(path, "type_mismatch", f"期望 object，实际 {type_name(value)}"))
            return
        check_object(value, node, path, issues, forbid_extra)
    elif expected == "array":
        if not isinstance(value, list):
            issues.append(mk(path, "type_mismatch", f"期望 array，实际 {type_name(value)}"))
            return
        n = len(value)
        if "minItems" in node and n < node["minItems"]:
            issues.append(mk(path, "array_too_short", f"元素 {n} 个，少于要求的 {node['minItems']} 个"))
        if "maxItems" in node and n > node["maxItems"]:
            issues.append(mk(path, "array_too_long", f"元素 {n} 个，多于允许的 {node['maxItems']} 个"))
        item_node = node.get("items")
        if item_node:
            for i, item in enumerate(value):
                check_value(item, item_node, f"{path}[{i}]", issues, forbid_extra)
    elif expected == "string":
        if not isinstance(value, str):
            issues.append(mk(path, "type_mismatch", f"期望 string，实际 {type_name(value)}"))
            return
        allow_empty = node.get("allowEmpty", False) or node.get("nonEmpty") is False
        if not allow_empty and not value.strip():
            issues.append(mk(path, "empty_string", "字段为空字符串"))
            return
        if "minLen" in node and len(value) < node["minLen"]:
            issues.append(mk(path, "too_short", f"长度 {len(value)}，少于建议的 {node['minLen']}", node.get("severity", "warn")))
        if "maxLen" in node and len(value) > node["maxLen"]:
            issues.append(mk(path, "too_long", f"长度 {len(value)}，超过 {node['maxLen']}"))
        if "pattern" in node and not re.search(node["pattern"], value):
            issues.append(mk(path, "pattern_mismatch", f"不匹配 /{node['pattern']}/：{value!r}"))
        if "enum" in node and value not in node["enum"]:
            issues.append(mk(path, "not_in_enum", f"取值 {value!r} 不在允许集合 {node['enum']} 内"))
    elif expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            issues.append(mk(path, "type_mismatch", f"期望 integer，实际 {type_name(value)}"))
            return
        if "min" in node and value < node["min"]:
            issues.append(mk(path, "out_of_range", f"{value} 小于下界 {node['min']}"))
        if "max" in node and value > node["max"]:
            issues.append(mk(path, "out_of_range", f"{value} 大于上界 {node['max']}"))
    elif expected == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            issues.append(mk(path, "type_mismatch", f"期望 number，实际 {type_name(value)}"))
    elif expected == "boolean":
        if not isinstance(value, bool):
            issues.append(mk(path, "type_mismatch", f"期望 boolean，实际 {type_name(value)}"))


def check_object(obj: dict, node: dict, base: str, issues: list[Issue], forbid_extra: bool) -> None:
    fields: dict[str, dict] = node.get("fields", {})
    for name in node.get("required", []):
        if name not in obj:
            issues.append(mk(f"{base}.{name}" if base else name, "missing_field", "缺少必填字段"))
    for name, sub in fields.items():
        if name in obj:
            check_value(obj[name], sub, f"{base}.{name}" if base else name, issues, forbid_extra)
    if node.get("forbidExtra", forbid_extra):
        for name in obj:
            if name not in fields:
                issues.append(
                    mk(f"{base}.{name}" if base else name, "extra_field", "参考格式中不存在该字段，需确认删除或纳入规格")
                )


# --------------------------------------------------------------------------
# 跨字段检查
# --------------------------------------------------------------------------


def normalize_variant(text: str, strip_chars: str, lowercase: bool) -> str:
    out = text.strip().strip(strip_chars) if strip_chars else text.strip()
    return out.lower() if lowercase else out


def build_allowed_set(entry: Any, check: dict) -> set[str]:
    allowed: set[str] = set()
    for _, sv in resolve(entry, check["source"]):
        if not isinstance(sv, str):
            continue
        pieces = sv.split(check.get("split", "/")) if check.get("split") else [sv]
        allowed.update(
            normalize_variant(piece, check.get("strip", ""), check.get("lowercase", True))
            for piece in pieces
            if piece.strip()
        )
    allowed.discard("")
    return allowed


def matches_allowed(value: str, allowed: set[str], check: dict) -> bool:
    norm = normalize_variant(value, check.get("strip", ""), check.get("lowercase", True))
    if not norm:
        return False
    if norm in allowed:
        return True
    return check.get("mode", "exact") == "lenient" and any(norm in a or a in norm for a in allowed)


def run_check(entry: Any, check: dict, issues: list[Issue]) -> None:

    kind = check.get("type")
    severity = check.get("severity", "error")
    cid = check.get("id", kind or "check")
    path = check.get("path", "")

    if kind == "must_contain":
        needle = check["substring"]
        for p, v in resolve(entry, path):
            if isinstance(v, str) and needle not in v:
                issues.append(mk(p, cid, f"缺少必需内容 {needle!r}", severity))

    elif kind == "regex":
        pattern = check["pattern"]
        for p, v in resolve(entry, path):
            if isinstance(v, str) and not re.search(pattern, v):
                issues.append(mk(p, cid, f"不匹配 /{pattern}/：{v!r}", severity))

    elif kind == "array_len":
        for p, v in resolve(entry, path):
            if not isinstance(v, list):
                continue
            if "equals" in check and len(v) != check["equals"]:
                issues.append(mk(p, cid, f"应为 {check['equals']} 项，实际 {len(v)} 项", severity))
            if "min" in check and len(v) < check["min"]:
                issues.append(mk(p, cid, f"至少 {check['min']} 项，实际 {len(v)} 项", severity))
            if "max" in check and len(v) > check["max"]:
                issues.append(mk(p, cid, f"至多 {check['max']} 项，实际 {len(v)} 项", severity))

    elif kind == "index_in_range":
        target = resolve(entry, check["of"])
        length = len(target[0][1]) if target and isinstance(target[0][1], list) else 0
        for p, v in resolve(entry, path):
            if isinstance(v, bool) or not isinstance(v, int):
                continue
            if not 0 <= v < length:
                issues.append(mk(p, cid, f"索引 {v} 越界，{check['of']} 共 {length} 项", severity))

    elif kind == "value_in_set_from":
        allowed = build_allowed_set(entry, check)
        if not allowed:
            return
        for p, v in resolve(entry, path):
            if not isinstance(v, str) or not v.strip():
                continue
            if not matches_allowed(v, allowed, check):
                issues.append(
                    mk(p, cid, f"{v!r} 不属于 {check['source']} 所列变体 {sorted(allowed)}", severity)
                )

    elif kind == "any_in_set_from":
        # 子项的若干候选字段里，至少要有一个命中条目级标识的变体集合。
        allowed = build_allowed_set(entry, check)
        if not allowed:
            return
        items = resolve(entry, check["itemPath"]) if check.get("itemPath") else [("", entry)]
        for item_path, item in items:
            candidates = [
                (p, v)
                for rel in check.get("paths", [])
                for p, v in resolve(item, rel)
                if isinstance(v, str)
            ]
            if candidates and not any(matches_allowed(v, allowed, check) for _, v in candidates):
                shown = "、".join(f"{rel}={v!r}" for rel, v in candidates)
                issues.append(
                    mk(
                        item_path or "-",
                        cid,
                        f"候选字段（{shown}）均未命中 {check['source']} 的变体 {sorted(allowed)}",
                        severity,
                    )
                )


    elif kind == "unique_local":
        seen: dict[str, str] = {}
        for p, v in resolve(entry, path):
            key = json.dumps(v, ensure_ascii=False, sort_keys=True)
            if key in seen:
                issues.append(mk(p, cid, f"与 {seen[key]} 重复：{v!r}", severity))
            else:
                seen[key] = p

    elif kind == "unique_global":
        pass  # 在 audit 层统一处理

    else:
        issues.append(mk(path or "-", "unknown_check", f"spec 里的 check 类型 {kind!r} 不被支持", "warn"))


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------


def entry_id(entry: Any, id_field: str | None, index: int, keys: list[str] | None) -> Any:
    if keys is not None:
        return keys[index]
    if id_field and isinstance(entry, dict) and id_field in entry:
        return entry[id_field]
    return index


def audit(args: argparse.Namespace) -> int:
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    container = args.container or spec.get("container", "array")
    id_field = args.id_field or spec.get("idField")
    forbid_extra = spec.get("forbidExtraFields", True)
    entry_node = spec.get("entry") or {"type": "object"}
    checks = spec.get("checks", [])

    data_path = Path(args.data)
    entries, keys = load_entries(data_path, container)

    all_issues: list[dict] = []
    dirty: set[int] = set()
    for index, entry in enumerate(entries):
        issues: list[Issue] = []
        check_value(entry, entry_node, "", issues, forbid_extra)
        for check in checks:
            run_check(entry, check, issues)
        if issues:
            dirty.add(index)
            eid = entry_id(entry, id_field, index, keys)
            for issue in issues:
                all_issues.append({"index": index, "id": eid, **issue})

    # 全局唯一性
    for check in checks:
        if check.get("type") != "unique_global":
            continue
        seen: dict[str, tuple[int, Any]] = {}
        for index, entry in enumerate(entries):
            for p, v in resolve(entry, check.get("path", "")):
                key = json.dumps(v, ensure_ascii=False, sort_keys=True)
                if key in seen:
                    prev_index, prev_id = seen[key]
                    dirty.add(index)
                    all_issues.append(
                        {
                            "index": index,
                            "id": entry_id(entry, id_field, index, keys),
                            "path": p,
                            "rule": check.get("id", "unique_global"),
                            "severity": check.get("severity", "error"),
                            "message": f"{v!r} 与条目 index={prev_index} (id={prev_id}) 重复",
                        }
                    )
                else:
                    seen[key] = (index, entry_id(entry, id_field, index, keys))

    by_rule: dict[str, int] = {}
    by_path: dict[str, int] = {}
    errors = warnings = 0
    for issue in all_issues:
        by_rule[issue["rule"]] = by_rule.get(issue["rule"], 0) + 1
        generic = re.sub(r"\[\d+\]", "[]", issue["path"])
        by_path[generic] = by_path.get(generic, 0) + 1
        if issue["severity"] == "error":
            errors += 1
        else:
            warnings += 1

    result = {
        "data": str(data_path),
        "spec": str(Path(args.spec)),
        "container": container,
        "idField": id_field,
        "entryCount": len(entries),
        "summary": {
            "errors": errors,
            "warnings": warnings,
            "entriesWithIssues": len(dirty),
            "cleanEntries": len(entries) - len(dirty),
            "byRule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
            "byPath": dict(sorted(by_path.items(), key=lambda kv: -kv[1])[:30]),
        },
        "semanticRules": spec.get("semanticRules", []),
        "dirtyIndexes": sorted(dirty),
        "issues": all_issues,
    }

    if args.out:
        write_json(Path(args.out), result)
    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(render_audit_md(result), encoding="utf-8")

    print(render_audit_md(result, max_examples=args.show))
    return 1 if errors else 0


def render_audit_md(result: dict, max_examples: int = 0) -> str:
    s = result["summary"]
    lines = [
        f"# 体检结果：{result['data']}",
        "",
        f"- 条目总数：{result['entryCount']}（干净 {s['cleanEntries']} / 有问题 {s['entriesWithIssues']}）",
        f"- error：{s['errors']}，warn：{s['warnings']}",
        "",
        "## 按规则聚合",
        "",
    ]
    lines += [f"- {rule}：{count}" for rule, count in s["byRule"].items()] or ["- 无问题"]
    lines += ["", "## 按字段路径聚合（Top）", ""]
    lines += [f"- {path}：{count}" for path, count in s["byPath"].items()] or ["- 无问题"]
    if result.get("semanticRules"):
        lines += ["", "## 需人工/子代理判断的语义规则（脚本不校验）", ""]
        lines += [f"- {rule}" for rule in result["semanticRules"]]
    if max_examples:
        lines += ["", f"## 样例问题（前 {max_examples} 条）", ""]
        for issue in result["issues"][:max_examples]:
            lines.append(
                f"- [{issue['severity']}] index={issue['index']} id={issue['id']} "
                f"{issue['path']} · {issue['rule']} · {issue['message']}"
            )
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# split
# --------------------------------------------------------------------------


def split(args: argparse.Namespace) -> int:
    audit_result = json.loads(Path(args.audit).read_text(encoding="utf-8")) if args.audit else {}
    container = args.container or audit_result.get("container", "array")
    id_field = args.id_field or audit_result.get("idField")
    entries, keys = load_entries(Path(args.data), container)

    issues_by_index: dict[int, list[dict]] = {}
    for issue in audit_result.get("issues", []):
        issues_by_index.setdefault(issue["index"], []).append(
            {k: issue[k] for k in ("path", "rule", "severity", "message")}
        )

    selected = list(range(len(entries)))
    if args.ids:
        wanted = {str(x) for x in args.ids.split(",")}
        selected = [i for i in selected if str(entry_id(entries[i], id_field, i, keys)) in wanted]
    elif args.only_dirty:
        selected = sorted(issues_by_index) or audit_result.get("dirtyIndexes", [])

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for shard_no, start in enumerate(range(0, len(selected), args.size)):
        chunk = selected[start : start + args.size]
        name = f"shard-{shard_no:03d}"
        payload = {
            "shard": name,
            "container": container,
            "idField": id_field,
            "entries": [
                {
                    "index": i,
                    "id": entry_id(entries[i], id_field, i, keys),
                    "issues": issues_by_index.get(i, []),
                    "entry": entries[i],
                }
                for i in chunk
            ],
        }
        write_json(out_dir / f"{name}.json", payload)
        manifest.append(
            {
                "shard": name,
                "file": f"{name}.json",
                "entryCount": len(chunk),
                "indexes": chunk,
                "issueCount": sum(len(issues_by_index.get(i, [])) for i in chunk),
            }
        )
    write_json(out_dir / "manifest.json", {"shardCount": len(manifest), "totalEntries": len(selected), "shards": manifest})
    print(f"已生成 {len(manifest)} 个分片，共 {len(selected)} 条条目 → {out_dir}")
    return 0


# --------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            flat.update(flatten(v, f"{prefix}.{k}" if prefix else k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            flat.update(flatten(v, f"{prefix}[{i}]"))
    else:
        flat[prefix or "."] = value
    return flat


def entry_changes(before: Any, after: Any) -> list[str]:
    fb, fa = flatten(before), flatten(after)
    changed = [p for p in fb if p not in fa or fa[p] != fb[p]]
    changed += [p for p in fa if p not in fb]
    return sorted(set(changed))


def merge(args: argparse.Namespace) -> int:
    container = args.container
    id_field = args.id_field
    if args.spec:
        spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
        container = container or spec.get("container", "array")
        id_field = id_field or spec.get("idField")
    container = container or "array"

    entries, keys = load_entries(Path(args.data), container)
    merged = [json.loads(json.dumps(e, ensure_ascii=False)) for e in entries]

    problems: list[str] = []
    applied: dict[int, list[str]] = {}
    seen_index: set[int] = set()

    files = sorted(Path(args.fixed_dir).glob("*.json"))
    files = [f for f in files if f.name != "manifest.json"]
    if not files:
        raise SystemExit(f"[错误] {args.fixed_dir} 下没有修复分片")

    for file in files:
        payload = json.loads(file.read_text(encoding="utf-8"))
        items = payload["entries"] if isinstance(payload, dict) else payload
        for item in items:
            if "index" not in item or "entry" not in item:
                problems.append(f"{file.name}: 条目缺少 index 或 entry 字段，已跳过")
                continue
            index = item["index"]
            if not 0 <= index < len(entries):
                problems.append(f"{file.name}: index={index} 越界（原数据 {len(entries)} 条），已跳过")
                continue
            if index in seen_index:
                problems.append(f"{file.name}: index={index} 被多个分片重复修复，保留后写入的版本")
            seen_index.add(index)
            new_entry = item["entry"]
            if id_field and isinstance(new_entry, dict) and isinstance(entries[index], dict):
                if new_entry.get(id_field) != entries[index].get(id_field):
                    problems.append(
                        f"{file.name}: index={index} 的 {id_field} 从 "
                        f"{entries[index].get(id_field)!r} 变为 {new_entry.get(id_field)!r}，已拒绝写入"
                    )
                    continue
            changes = entry_changes(entries[index], new_entry)
            if changes:
                applied[index] = changes
            merged[index] = new_entry

    expected = None
    manifest_path = Path(args.fixed_dir).parent / "shards" / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = {i for shard in manifest["shards"] for i in shard["indexes"]}
        missing = sorted(expected - seen_index)
        if missing:
            problems.append(f"以下条目应被修复但分片缺失（保留原值）：{missing[:50]}")

    if len(merged) != len(entries):
        problems.append(f"条目数从 {len(entries)} 变为 {len(merged)}，合并异常")

    newline = detect_newline(Path(args.data)) if args.newline == "keep" else (
        "\r\n" if args.newline == "crlf" else "\n"
    )
    dump_entries(Path(args.out), merged, keys, container, newline=newline)

    path_counter: dict[str, int] = {}
    for changes in applied.values():
        for p in changes:
            generic = re.sub(r"\[\d+\]", "[]", p)
            path_counter[generic] = path_counter.get(generic, 0) + 1

    lines = [
        f"# 合并报告：{args.out}",
        "",
        f"- 原始条目：{len(entries)}，合并后条目：{len(merged)}",
        f"- 收到修复条目：{len(seen_index)}，实际发生变更：{len(applied)}",
        "",
        "## 变更字段路径（Top 30）",
        "",
    ]
    lines += [f"- {p}：{c}" for p, c in sorted(path_counter.items(), key=lambda kv: -kv[1])[:30]] or ["- 无变更"]
    lines += ["", "## 守卫告警", ""]
    lines += [f"- {p}" for p in problems] or ["- 无"]
    report = "\n".join(lines) + "\n"

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(report, encoding="utf-8")
    print(report)
    return 1 if problems else 0


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------


def diff(args: argparse.Namespace) -> int:
    container = args.container or "array"
    before, keys = load_entries(Path(args.before), container)
    after, _ = load_entries(Path(args.after), container)

    lines = [f"# 变更对比：{args.before} → {args.after}", ""]
    if len(before) != len(after):
        lines.append(f"**条目数变化：{len(before)} → {len(after)}**")
        lines.append("")

    path_counter: dict[str, int] = {}
    changed_entries = []
    for i in range(min(len(before), len(after))):
        changes = entry_changes(before[i], after[i])
        if not changes:
            continue
        changed_entries.append((i, entry_id(after[i], args.id_field, i, keys), changes))
        for p in changes:
            generic = re.sub(r"\[\d+\]", "[]", p)
            path_counter[generic] = path_counter.get(generic, 0) + 1

    lines += [f"- 发生变更的条目：{len(changed_entries)} / {len(before)}", "", "## 变更字段路径（Top 30）", ""]
    lines += [f"- {p}：{c}" for p, c in sorted(path_counter.items(), key=lambda kv: -kv[1])[:30]] or ["- 无变更"]
    lines += ["", f"## 变更条目明细（前 {args.show} 条）", ""]
    for i, eid, changes in changed_entries[: args.show]:
        preview = "、".join(changes[:8]) + ("…" if len(changes) > 8 else "")
        lines.append(f"- index={i} id={eid}：{preview}")
    report = "\n".join(lines) + "\n"

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(report, encoding="utf-8")
    print(report)
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="结构化数据集体检 / 分片 / 合并 / 对比")
    sub = parser.add_subparsers(dest="command", required=True)

    common_container = dict(default=None, choices=["array", "jsonl", "object_map"])

    p_audit = sub.add_parser("audit", help="按 spec 全量校验")
    p_audit.add_argument("--data", required=True)
    p_audit.add_argument("--spec", required=True)
    p_audit.add_argument("--container", **common_container)
    p_audit.add_argument("--id-field", default=None)
    p_audit.add_argument("--out", default=None, help="issues 与摘要写入的 JSON 路径")
    p_audit.add_argument("--report", default=None, help="Markdown 摘要写入路径")
    p_audit.add_argument("--show", type=int, default=15, help="终端展示的样例问题条数")
    p_audit.set_defaults(func=audit)

    p_split = sub.add_parser("split", help="按条目分片")
    p_split.add_argument("--data", required=True)
    p_split.add_argument("--audit", default=None, help="audit 输出的 JSON，用于携带 issues")
    p_split.add_argument("--out-dir", required=True)
    p_split.add_argument("--size", type=int, default=8)
    p_split.add_argument("--only-dirty", action="store_true", help="只分片有问题的条目")
    p_split.add_argument("--ids", default=None, help="逗号分隔的条目 id 白名单（针对性模式）")
    p_split.add_argument("--container", **common_container)
    p_split.add_argument("--id-field", default=None)
    p_split.set_defaults(func=split)

    p_merge = sub.add_parser("merge", help="合并修复分片")
    p_merge.add_argument("--data", required=True, help="原始数据文件")
    p_merge.add_argument("--fixed-dir", required=True, help="子代理写回的分片目录")
    p_merge.add_argument("--out", required=True)
    p_merge.add_argument("--spec", default=None)
    p_merge.add_argument("--report", default=None)
    p_merge.add_argument("--container", **common_container)
    p_merge.add_argument("--id-field", default=None)
    p_merge.add_argument("--newline", default="keep", choices=["keep", "lf", "crlf"])
    p_merge.set_defaults(func=merge)

    p_diff = sub.add_parser("diff", help="对比两份数据集")
    p_diff.add_argument("--before", required=True)
    p_diff.add_argument("--after", required=True)
    p_diff.add_argument("--container", **common_container)
    p_diff.add_argument("--id-field", default="id")
    p_diff.add_argument("--out", default=None)
    p_diff.add_argument("--show", type=int, default=30)
    p_diff.set_defaults(func=diff)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
