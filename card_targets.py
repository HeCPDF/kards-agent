#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""card_targets.py —— **进程外**跑卡牌的"能不能被指向"判据（R8 的正当路线）。

为什么这么做
============
游戏的合法性判定分成三段，**两段是蓝图**（在 FModel 导出里可读可移植），
只有最里面一层是原生：

```
IsValidHandTarget（20 张卡覆写，蓝图）        ← 出牌卡自筛："谁能当我的手牌目标"
CanSelectAsTarget（Content\\Library\\cardsCheckFunctions.cpp，蓝图 Wrapper）
    ├─ 费用 / 阵营（读字段即可）
    ├─ Targeted->CanBeTargetted(...)         ← **原生**：0x4A7F910，0 张卡覆写
    ├─ isSuppressed || canIt                 ← 被压制则放行
    └─ CanOtherCardBeTargetted(...)          ← 1 张卡覆写（card_unit_no_3_commando）
```

`UBaseCardObject` 上的这些字段我们**已经在读**（type/位置/阵营/费用/压制/税/关键词/被贴效果），
所以只要把蓝图那两段**在进程外跑一遍**，就能得到权威的"可指向集合"，
不必再去调用 exe 里的函数（那要注入线程，越红线）。

本工具做三件事
==============
1. **抽**：从 `exports-1.58.27125.Steam\\kards\\Content` 里抽出所有 `IsValidHandTarget` /
   `CanOtherCardBeTargetted` / `CanBeTargetted` / `CanPlayFromHand` 覆写体 + wrapper。
2. **跑**：一个小 VM 执行 FModel 导出的伪 C++（表达式 + 赋值 + if/goto/label + 调用），
   调用映射到自己实现的"原生原语"（`IsUnit` / `getTotalKreditCost` / …）。
3. **报覆盖**：哪些语句/原语还没实现（`--coverage`），以及逐卡的可执行率。

用法
====
    python card_targets.py --list                 # 有哪些覆写（20/1/0/438）
    python card_targets.py --show <card_name>     # 打印某张卡的 IsValidHandTarget 源码
    python card_targets.py --coverage             # VM 对 20 个覆写的可执行率 + 缺哪些原语
    python card_targets.py --check <snapshot.json> --hand-card <cardID|名字>
                                                  # 用内存快照算"这张手牌能指向谁"
（快照用 `python -m kardsmem dump --out snap.json` 生成。）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agentpath  # noqa: E402  —— 工作区根按标志物找，别硬编码

WORKSPACE = agentpath.workspace()
EXPORT = os.path.join(WORKSPACE, r"reverse-data\exports-1.58.27125.Steam\kards\Content")
CARDS_DIR = os.path.join(EXPORT, r"Blueprints\Cards")
WRAPPER = os.path.join(EXPORT, r"Library\cardsCheckFunctions.cpp")

TARGET_FUNCS = ("IsValidHandTarget", "CanBeTargetted", "CanOtherCardBeTargetted")
UNIT_TYPES = {"tank", "fighter", "bomber", "infantry", "artillery", "antiair",
              "antitank", "tankdestroyer"}


# --------------------------------------------------------------------------
# 1) 抽取
# --------------------------------------------------------------------------
def extract_body(text: str, fname: str):
    """从伪 C++ 里抠出某个函数的函数体（花括号配对）。"""
    m = re.search(r"\b(?:void|bool|int32)\s+%s\s*\([^)]*\)\s*\{" % re.escape(fname), text)
    if not m:
        return None
    i = m.end() - 1
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j]
    return None


def iter_cpp(root):
    for dp, _d, fs in os.walk(root):
        for fn in fs:
            if fn.endswith(".cpp"):
                yield os.path.join(dp, fn)


def collect(which=TARGET_FUNCS):
    """→ [(名字, 文件, 函数名, 函数体)]"""
    out = []
    for path in iter_cpp(CARDS_DIR):
        try:
            txt = open(path, "r", encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        for fname in which:
            body = extract_body(txt, fname)
            if body is not None:
                out.append((os.path.basename(path)[:-4], path, fname, body))
    return out


# --------------------------------------------------------------------------
# 2) 伪 C++ → 语句 / 表达式 解析 + VM
# --------------------------------------------------------------------------
CALL_RE = re.compile(r"(?:([A-Za-z_][\w:]*)\s*->\s*|([A-Za-z_][\w:]*)::)([A-Za-z_]\w*)\s*\((.*)\)\s*$")


def split_args(s: str):
    args, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        args.append(cur.strip())
    return args


class Unknown(Exception):
    """遇到没实现的调用/语法 —— 覆盖统计用，不是崩溃。"""


def parse_lines(body: str):
    stmts = []
    for raw in body.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith("//"):
            continue
        if ln.startswith("Label_"):
            stmts.append(("label", ln.rstrip(":")))
        elif ln == "return;":
            stmts.append(("return", None))
        elif ln.startswith("if ("):
            m = re.match(r"if\s*\((.*)\)\s*goto\s+(Label_\w+);", ln)
            if m:
                stmts.append(("ifgoto", (m.group(1), m.group(2))))
            else:
                stmts.append(("other", ln))
        elif ln.startswith("else"):
            stmts.append(("other", ln))
        elif ln.startswith("goto "):
            stmts.append(("goto", ln.split()[1].rstrip(";")))
        elif "=" in ln and ln.endswith(";"):
            lhs, _, rhs = ln[:-1].partition("=")
            stmts.append(("assign", (lhs.strip(), rhs.strip())))
        elif ln.endswith(");"):
            stmts.append(("call", ln[:-1]))
        else:
            stmts.append(("other", ln))
    return stmts


class VM:
    """极小的伪 C++ 求值器。任何不认识的构造 → Unknown（计入覆盖缺口）。"""

    def __init__(self, natives: dict, card: dict, ctx: dict):
        self.natives = natives      # 名 → callable(vm, args) -> value
        self.card = card            # 当前"出牌卡"（CDO 侧字段）
        self.ctx = ctx              # 盘面等环境
        self.env = {}
        self.missing = set()

    # -- 表达式 ----------------------------------------------------------
    def value(self, expr: str):
        e = expr.strip()
        while e.startswith("(") and e.endswith(")") and self._balanced(e):
            e = e[1:-1].strip()
        if e in ("true", "TRUE"):
            return True
        if e in ("false", "FALSE"):
            return False
        if re.fullmatch(r"-?(0x[0-9A-Fa-f]+|\d+)", e):
            return int(e, 0)
        if re.fullmatch(r'"(?:[^"\\]|\\.)*"', e):
            return e[1:-1]
        if e.startswith("!"):
            return not bool(self.value(e[1:]))
        for op in ("&&", "||"):
            parts = self._split_op(e, op)
            if parts:
                vals = [self.value(p) for p in parts]
                return all(vals) if op == "&&" else any(vals)
        for op in ("!==", "===", "==", "!=", "<=", ">=", "<", ">", "+", "-"):
            parts = self._split_op(e, op)
            if parts:
                a, b = self.value(parts[0]), self.value(parts[1])
                norm = {"!==": "!=", "===": "=="}.get(op, op)
                try:
                    return {"==": a == b, "!=": a != b, "<=": a <= b, ">=": a >= b,
                            "<": a < b, ">": a > b, "+": a + b, "-": a - b}[norm]
                except TypeError:
                    raise Unknown("op %s on %r/%r" % (op, a, b))
        m = CALL_RE.match(e)
        if m:
            owner, ns, fname, argstr = m.group(1), m.group(2), m.group(3), m.group(4)
            args = [] if argstr.strip() in ("", "0x0") else split_args(argstr)
            if ns:                                    # UFunctionLibrary::Foo(...)
                key = "%s::%s" % (ns.split("::")[0], fname)
            else:
                key = fname
            fn = self.natives.get(key)
            if fn is None:
                self.missing.add(key)
                raise Unknown("native %s not implemented" % key)
            vals = []
            for a in args:
                a = a.strip().strip("&")
                if re.fullmatch(r"[A-Za-z_]\w*", a):
                    vals.append(self.env.get(a, a))   # 出参变量按"名"传，函数回写
                else:
                    vals.append(self.value(a))
            return fn(self, args, vals)
        if re.fullmatch(r"[\w:]+", e):                # 枚举常量 / 变量名 / self 成员
            if e in self.env:
                return self.env[e]
            if e in self.card:                        # 蓝图里 self 成员可能写作裸名
                return self.card[e]
            if "::" in e:
                return e
            raise Unknown("free var %s" % e)
        m2 = re.match(r"^([A-Za-z_]\w*)-\s*>\s*([A-Za-z_]\w*)$", e)   # obj->属性（蓝图取成员）
        if m2:
            prop = m2.group(2)
            if prop in self.card:
                return self.card[prop]
            p2 = {"faction": "faction", "Type": "card_type", "rarity": "rarity",
                  "attack": "attack", "defense": "defense", "kredits": "kredit_cost",
                  "operationCost": "operation_cost", "isSuppressed": "is_suppressed",
                  "KreditsTax_AsEnemyTarget": "kredits_tax_as_enemy_target"}.get(prop)
            if p2 and p2 in self.card:
                return self.card[p2]
            raise Unknown("prop %s" % prop)
        raise Unknown("expr %r" % e)

    @staticmethod
    def _balanced(e):
        depth = 0
        for i, ch in enumerate(e):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(e) - 1:
                    return False
        return True

    def _split_op(self, e, op):
        depth = 0
        i = 0
        while i < len(e):
            ch = e[i]
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif depth == 0 and e.startswith(op, i):
                if op == "-" and i + 1 < len(e) and e[i + 1] == ">":
                    i += 1                     # `obj->Fn()` 的箭头，不是减号
                    continue
                if op == ">" and i > 0 and e[i - 1] == "-":
                    i += 1                     # 同上（箭头里的 '>'）
                    continue
                if op == "-" and i > 0 and e[i - 1] in "+-*/:(,":
                    i += 1                     # 一元负号
                    continue
                a, b = e[:i].strip(), e[i + len(op):].strip()
                if a and b and not (op in ("+", "-") and i == 0):
                    return (a, b) if op in ("==", "!=", "<=", ">=", "<", ">", "+", "-") \
                        else [p.strip() for p in self._split_all(e, op)]
            i += 1
        return None

    def _split_all(self, e, op):
        out, depth, start, i = [], 0, 0, 0
        while i < len(e):
            ch = e[i]
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif depth == 0 and e.startswith(op, i):
                out.append(e[start:i])
                start = i + len(op)
                i = start - 1
            i += 1
        out.append(e[start:])
        return out

    # -- 语句 ------------------------------------------------------------
    def run(self, stmts, out_names):
        labels = {s[1]: i for i, s in enumerate(stmts) if s[0] == "label"}
        i, guard = 0, 0
        while i < len(stmts) and guard < 5000:
            guard += 1
            kind, arg = stmts[i]
            if kind == "assign":
                lhs, rhs = arg
                val = self.value(rhs)
                self.env[lhs] = val
                if lhs in out_names:
                    return self.env
            elif kind == "call":
                self.value(arg)
            elif kind == "ifgoto":
                cond, label = arg
                if bool(self.value(cond)):
                    i = labels.get(label, i)
                    continue
            elif kind == "goto":
                i = labels.get(arg, i)
                continue
            elif kind == "label":
                pass
            elif kind == "return":
                return self.env
            i += 1
        return self.env


# --------------------------------------------------------------------------
# 3) 原语（把虚拟机需要的"游戏函数"映射到内存快照字段）
# --------------------------------------------------------------------------
def make_natives():
    N = {}

    def out(vm, args, vals, value=True):
        """把结果写回调用点给出的出参变量名。"""
        if args:
            vm.env[args[-1].strip()] = value
        return value

    def simple(name, fn):
        def wrapper(vm, args, vals):
            return out(vm, args, fn(vm, args, vals))
        N[name] = wrapper
        return wrapper

    simple("IsUnit", lambda vm, a, v: vm.card.get("card_type") in UNIT_TYPES)
    simple("IsAirUnit", lambda vm, a, v: vm.card.get("card_type") in ("fighter", "bomber"))
    simple("IsTank", lambda vm, a, v: vm.card.get("card_type") == "tank")
    simple("IsBomber", lambda vm, a, v: vm.card.get("card_type") == "bomber")
    simple("IsFighter", lambda vm, a, v: vm.card.get("card_type") == "fighter")
    simple("IsInfantry", lambda vm, a, v: vm.card.get("card_type") == "infantry")
    simple("IsArtillery", lambda vm, a, v: vm.card.get("card_type") == "artillery")
    simple("IsAntiAir", lambda vm, a, v: vm.card.get("card_type") == "antiair")
    simple("IsAntiTank", lambda vm, a, v: vm.card.get("card_type") == "antitank")
    simple("IsOrder", lambda vm, a, v: vm.card.get("card_type") == "order")
    simple("IsGotcha", lambda vm, a, v: vm.card.get("card_type") in ("gotcha", "wildcard"))
    simple("IsLocation", lambda vm, a, v: vm.card.get("card_type") == "location")
    simple("getTotalKreditCost", lambda vm, a, v: vm.card.get("kredit_cost"))
    simple("getTotalAttack", lambda vm, a, v: vm.card.get("attack"))
    simple("getTotalDefense", lambda vm, a, v: vm.card.get("defense"))
    simple("getTotalOperationCost", lambda vm, a, v: vm.card.get("operation_cost"))
    simple("getAndDecryptKredit", lambda vm, a, v: vm.card.get("kredit"))
    simple("getAndDecryptAttack", lambda vm, a, v: vm.card.get("attack"))
    simple("IsSuppressed", lambda vm, a, v: vm.card.get("is_suppressed"))
    simple("IsReserved", lambda vm, a, v: vm.card.get("is_reserved"))

    def _enum(x):
        return str(x).split("::")[-1] if x is not None else None

    N["UFunctionLibrary::EnumCompareFaction"] = \
        lambda vm, args, vals: out(vm, args, _enum(vm.card.get("faction")) == _enum(
            vals[1] if len(vals) > 1 else None))
    N["UFunctionLibrary::EnumCompareType"] = \
        lambda vm, args, vals: out(vm, args, _enum(vm.card.get("card_type")) == _enum(
            vals[1] if len(vals) > 1 else None))
    N["UFunctionLibrary::EnumCompareRarity"] = \
        lambda vm, args, vals: out(vm, args, _enum(vm.card.get("rarity")) == _enum(
            vals[1] if len(vals) > 1 else None))
    return N


# --------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="进程外跑卡牌的指向判据（R8）")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--show", metavar="CARD")
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--check", metavar="SNAPSHOT", help="kardsmem dump 的快照 JSON")
    ap.add_argument("--hand-card", metavar="ID")
    a = ap.parse_args(argv)

    if not os.path.isdir(CARDS_DIR):
        print("找不到导出目录：%s" % CARDS_DIR)
        return 2

    if a.list or a.show or a.coverage:
        for fname in TARGET_FUNCS:
            hits = collect((fname,))
            print("%-26s %3d 处覆写" % (fname, len(hits)))
            if a.show:
                for name, path, fn, body in hits:
                    if a.show in name:
                        print("\n===== %s :: %s =====" % (name, fn))
                        for ln in body.splitlines():
                            if ln.strip():
                                print("   " + ln.strip())
        if a.coverage:
            hits = collect(("IsValidHandTarget",))
            total = runnable = 0
            missing = {}
            for name, path, fn, body in hits:
                stmts = parse_lines(body)
                total += 1
                try:
                    vm = VM(make_natives(), {"card_type": "infantry", "kredit_cost": 2, "faction": "USA", "rarity": "Common", "attack": 2, "defense": 2, "kredit": 2, "operation_cost": 0, "is_suppressed": False, "is_reserved": False,"cards": []},
                            {"cards": []})
                    vm.run(stmts, {"isIt"})
                    runnable += 1
                except Unknown as e:
                    missing.setdefault(str(e), []).append(name)
            print("\n=== IsValidHandTarget 可执行率 ===")
            print("  %d/%d 能跑（%d%%）" % (runnable, total, 100 * runnable // max(1, total)))
            for k, v in sorted(missing.items(), key=lambda kv: -len(kv[1])):
                print("  缺 %-40s 影响 %d 张：%s" % (k, len(v), ", ".join(v[:4])))
        return 0

    if a.check:
        print("（--check 待接：读 snapshot.json → 组 VM 环境 → 对每个候选跑 IsValidHandTarget）")
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
