#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""kardsmem.vm —— Kismet 字节码的**纯求值器**。

拼图的最后一块：
    kismet.py      把 UFunction 的字节码反汇编成 Expr 树
    kismetlib.py   通用原生原语（KismetMath/Array/String/…）
    cardnatives.py 游戏自有原生原语（UBaseCardObject::*）
    **vm.py**      把树跑起来

红线
====
**只读。** `EX_Let*` 一律写进**影子堆**（本帧的 dict），永远不回写游戏内存。
读字段走只读的 `ReadProcessMemory`。求值器里没有任何一条写内存的路径。

怎么对待"算不出来"
==================
宁可**响亮地停下**，也不给错答案（这正是 §7.6f「判据只挑不判」的由来）：

* 原语没实现 → `Unimplemented`
* 有副作用的原语 → `ImpureCall`
* 字段读不到 / 上下文缺失 → `Unimplemented`
* opcode 没支持 → `Unsupported`

`run()` 把停下的原因连同**已经走过的轨迹**一起返回，便于补齐而不是干瞪眼。

    from kardsmem.vm import VM
    vm = VM(session, get_field=my_field_reader)
    r = vm.run(ufunc_addr, self_obj=card_ptr, args={"cardID": 42})
    r["out"]      # 出参
    r["stopped"]  # None 表示跑完了；否则是停下的原因
    r["trace"]    # 走过的语句
"""
from __future__ import annotations

from typing import Optional

from . import kismet
from .kismetlib import ImpureCall, Unimplemented, arity as native_arity, call as native_call


class Unsupported(RuntimeError):
    """这个 opcode 求值器还没支持。"""


class _Ref:
    """左值：一个可赋值的位置。`kind` ∈ local / out / instance。"""

    __slots__ = ("kind", "name", "obj")

    def __init__(self, kind, name, obj=None):
        self.kind, self.name, self.obj = kind, name, obj

    def __repr__(self):
        return "%s:%s" % (self.kind, self.name)


class Frame:
    """一次调用的影子堆。**不碰游戏内存。**"""

    def __init__(self, self_obj=None, args=None):
        self.self_obj = self_obj
        self.locals = dict(args or {})
        self.out = {}
        # 被 Let 改写过的实例字段：写到这里，不写回进程
        self.shadow = {}

    def get(self, ref: _Ref, vm):
        if ref.kind in ("local", "out"):
            if ref.name in self.out:
                return self.out[ref.name]
            return self.locals.get(ref.name)
        key = (id(ref.obj) if ref.obj is not None else None, ref.name)
        if key in self.shadow:
            return self.shadow[key]
        return vm.read_field(ref.obj if ref.obj is not None else self.self_obj, ref.name)

    def set(self, ref: _Ref, value):
        if ref.kind == "out":
            self.out[ref.name] = value
        elif ref.kind == "local":
            self.locals[ref.name] = value
        else:
            self.shadow[(id(ref.obj) if ref.obj is not None else None, ref.name)] = value


class VM:
    """`session` 只要有 `.m` / `.base` / `.names_pool()`（转储也行）。

    `get_field(obj, name)` 由调用方注入：给定一个对象和字段名，返回值。
    不注入时只能跑不读字段的函数 —— 读字段会抛 `Unimplemented`，这是**故意的**。
    """

    MAX_STEPS = 20000          # 防止字节码里的环把我们转死

    def __init__(self, session, get_field=None, card_natives=None,
                 hooks: Optional[dict] = None):
        self.s = session
        self.resolve = kismet.Resolve(session)
        self.get_field = get_field
        self.cn = card_natives
        self.hooks = dict(hooks or {})      # "FuncName" -> callable(vm, frame, args)
        self._params = {}                   # UFunction -> [(形参名, 是否出参, 类型)]
        self._stack = set()                 # 调用栈上的 UFunction（判环）
        self.trace = []
        self.depth = 0

    # ---- 读字段 ----
    def read_field(self, obj, name):
        if self.get_field is None:
            raise Unimplemented("读字段 %r 需要注入 get_field" % name)
        return self.get_field(obj, name)

    # ---- 入口 ----
    def run(self, ufunc: int, self_obj=None, args: Optional[dict] = None) -> dict:
        code = kismet.script_of(self.s, ufunc)
        if code is None:
            return {"out": {}, "stopped": "原生函数，没有字节码", "trace": []}
        exprs, _used = kismet.disasm(code, self.resolve)
        index = {e.at: i for i, e in enumerate(exprs)}
        frame = Frame(self_obj, args)
        # ★ 执行流栈（EX_PushExecutionFlow / PopExecutionFlow / PopExecutionFlowIfNot）。
        #   蓝图的 Sequence / 多出口节点编译出来就靠它：Push 压一个"回来以后从哪继续"，
        #   Pop 弹出来跳过去。2026-09-23 实机跑 cardsCheckFunctions::CanAttack 时
        #   撞上的 —— 之前 10861 个函数全过是因为它们没用到这条路。
        self.flow = []
        self.approx = set()   # 这次求值用到的**近似**原语
        self.trace = []
        stopped = None
        i = steps = 0
        while i < len(exprs):
            steps += 1
            if steps > self.MAX_STEPS:
                stopped = "步数超过 %d，疑似死循环" % self.MAX_STEPS
                break
            e = exprs[i]
            self.trace.append("%5d %s" % (e.at, e.op))
            try:
                jump = self.stmt(e, frame)
            except (Unimplemented, ImpureCall, Unsupported) as ex:
                stopped = "%s @0x%X %s: %s" % (type(ex).__name__, e.at, e.op, ex)
                break
            except Exception as ex:                          # noqa: BLE001
                # ★ 原语拿到不该拿的类型（比如把 'local' 喂进 EqualEqual_ByteByte）
                #   要变成**一条可诊断的停止**，不是一个栈回溯 ——
                #   求值器的契约是"要么给对答案，要么说清为什么给不出"。
                stopped = "%s @0x%X %s: %s" % (type(ex).__name__, e.at, e.op, ex)
                break
            if jump == "return":
                break
            if isinstance(jump, int):
                if jump not in index:
                    stopped = "跳转目标 %d 不在语句边界上" % jump
                    break
                i = index[jump]
                continue
            i += 1
        return {"out": dict(frame.out), "locals": dict(frame.locals),
                "stopped": stopped, "trace": self.trace, "frame": frame,
                "approx": sorted(self.approx)}

    # ---- 语句 ----
    def stmt(self, e, f):
        """→ None 顺序执行 / int 跳到该偏移 / "return" 结束。"""
        op = e.op
        if op in ("Jump",):
            return e.args["to"]
        if op == "JumpIfNot":
            return e.args["to"] if not self.truthy(self.eval(e.kids[0], f)) else None
        if op == "PushExecutionFlow":
            self.flow.append(e.args["to"])
            return None
        if op == "PopExecutionFlow":
            if not self.flow:
                # 栈空还要弹 ⇒ 这条执行流走完了。引擎的行为是结束本次执行。
                return "return"
            return self.flow.pop()
        if op == "PopExecutionFlowIfNot":
            # 条件为**假**才弹栈跳走；为真则顺序执行（名字就是这个意思）。
            if self.truthy(self.eval(e.kids[0], f)):
                return None
            if not self.flow:
                return "return"
            return self.flow.pop()
        if op in ("Return", "EndOfScript"):
            return "return"
        if op in ("Nothing", "Tracepoint", "WireTracepoint", "Breakpoint",
                  "NothingInt32", "EndParmValue"):
            return None
        if op in ("Let", "LetBool", "LetObj", "LetWeakObjPtr", "LetDelegate",
                  "LetMulticastDelegate"):
            ref = self.lvalue(e.kids[0], f)
            f.set(ref, self.eval(e.kids[1], f))
            return None
        # 其余当表达式求值（调用、上下文调用……）
        self.eval(e, f)
        return None

    @staticmethod
    def truthy(v):
        return bool(v)

    # ---- 左值 ----
    def lvalue(self, e, f) -> _Ref:
        if e.op == "LocalOutVariable":
            return _Ref("out", e.args["prop"])
        if e.op == "LocalVariable":
            return _Ref("local", e.args["prop"])
        if e.op in ("InstanceVariable", "DefaultVariable", "ClassSparseDataVariable"):
            return _Ref("instance", e.args["prop"], None)
        if e.op == "Context":
            obj = self.eval(e.kids[0], f)
            inner = e.kids[1]
            if inner.op in ("InstanceVariable", "LocalVariable"):
                return _Ref("instance", inner.args["prop"], obj)
        raise Unsupported("不是可赋值的位置：%s" % e.op)

    # ---- 表达式 ----
    def eval(self, e, f):
        op = e.op
        if op == "True":
            return True
        if op == "False":
            return False
        if op in ("NoObject", "NoInterface", "Nothing"):
            return None
        if op == "Self":
            return f.self_obj
        if op == "IntZero":
            return 0
        if op == "IntOne":
            return 1
        if op in ("IntConst", "IntConstByte", "ByteConst", "Int64Const",
                  "UInt64Const", "FloatConst", "DoubleConst", "StringConst",
                  "UnicodeStringConst", "NameConst", "SkipOffsetConst",
                  "VectorConst", "Vector3fConst", "RotationConst"):
            return e.args.get("v")
        if op == "ObjectConst":
            # ★ 求值一律给**指针**（`EX_Context` 的对象表达式要能真的取到那个 UObject）。
            #   名字留在 `args["v"]` 里给反汇编和 hook 用（比如按枚举名认 ETypeEnum）。
            return e.args.get("ptr") or e.args.get("v")
        if op == "TextConst":
            # 文本常量：取第一个字符串子表达式，够判据用了
            for k in e.kids:
                v = self.eval(k, f)
                if isinstance(v, str):
                    return v
            return ""
        if op in ("LocalVariable", "LocalOutVariable", "InstanceVariable",
                  "DefaultVariable", "ClassSparseDataVariable"):
            return f.get(self.lvalue(e, f), self)
        if op in ("Context", "Context_FailSilent", "ClassContext", "InterfaceContext"):
            return self.ctx_call(e, f)
        if op in ("CallMath", "FinalFunction", "LocalFinalFunction",
                  "VirtualFunction", "LocalVirtualFunction"):
            return self.call(e, f, obj=f.self_obj)
        if op in ("DynamicCast", "MetaCast", "ObjToInterfaceCast",
                  "CrossInterfaceCast", "InterfaceToObjCast", "Cast"):
            return self.eval(e.kids[0], f)          # 判据里当作透传
        if op == "StructMemberContext":
            base = self.eval(e.kids[0], f)
            name = e.args["prop"]
            if isinstance(base, dict):
                return base.get(name)
            raise Unimplemented("结构体成员 %s（基对象不是 dict）" % name)
        if op == "SwitchValue":
            return self.switch(e, f)
        if op == "ArrayGetByRef":
            arr = self.eval(e.kids[0], f)
            i = self.eval(e.kids[1], f)
            return arr[i] if (arr and isinstance(i, int) and 0 <= i < len(arr)) else None
        if op == "Skip":
            return self.eval(e.kids[0], f)
        raise Unsupported("表达式 opcode %s" % op)

    def switch(self, e, f):
        val = self.eval(e.kids[0], f)
        rest = e.kids[1:]
        # kids 结构：[被switch的值] + n*(case值, case结果) + [default]
        for i in range(0, len(rest) - 1, 2):
            if self.eval(rest[i], f) == val:
                return self.eval(rest[i + 1], f)
        return self.eval(rest[-1], f) if rest else None

    # ---- 调用 ----
    def ctx_call(self, e, f):
        """`EX_Context`：先算出上下文对象，再在它身上执行内层表达式。"""
        obj = self.eval(e.kids[0], f)
        inner = e.kids[1]
        if inner.op in ("InstanceVariable", "LocalVariable", "DefaultVariable"):
            return f.get(_Ref("instance", inner.args["prop"], obj), self)
        if inner.op in ("CallMath", "FinalFunction", "LocalFinalFunction",
                        "VirtualFunction", "LocalVirtualFunction"):
            if obj is None and e.op != "Context_FailSilent":
                raise Unimplemented("在 None 上调用 %s" % inner.args.get("fn"))
            return self.call(inner, f, obj=obj)
        return self.eval(inner, f)

    def call(self, e, f, obj=None):
        fn = e.args.get("fn")
        name = fn if isinstance(fn, str) else self.resolve.obj(fn, "func")
        args = [self.eval(k, f) for k in e.kids]

        # 1) 调用方注入的钩子优先（比如"当前指向的是哪张卡"这种 UI 态）
        if name in self.hooks:
            return self.hooks[name](self, f, obj, args, e)

        # 2) 游戏自有原语（UBaseCardObject::*）
        #    ★ Kismet 把**出参也压进调用点**（`IsUnit(bool* isIt)` 在字节码里是一个
        #      带 1 个参数的调用）⇒ 得把入参和出参分开。
        #
        #    ★★ **不要假设"出参一律在末尾"。** 有的原生函数出参在**前面**：
        #        CanBeTargetted(bool* canIt, FString* Reason, FString* p1, FString* p2,
        #                       UBaseCardObject* targettingCard, bool byPlayFromHand)
        #        —— 前四个全是出参。按"前 n 个是入参"切会把出参当入参喂进去。
        #    ⇒ 只要拿得到 `UFunction`，就按**声明的 Parm 标志**逐个分（`_split_parms`），
        #      拿不到才退回"按 arity 切前 n 个"这个旧启发式。
        if self.cn is not None and self.cn.has(name):
            ins, outs = self._split_parms(
                fn, args, e, self.cn.arity(name),
                obj=obj if obj is not None else f.self_obj, name=name)
            v = self.cn.call(name, obj if obj is not None else f.self_obj, *ins)
            # ★ 有的原语是**我们自己写的近似**（游戏那边是原生代码，读不到）。
            #   用到了就记下来，让最终结果能标出"这个答案里掺了近似判据"。
            #   不标的话，近似会悄悄冒充"跑了游戏自己的逻辑"。
            if name in getattr(self.cn, "APPROX", ()):
                self.approx.add(name)
            self._write_outs(outs, f, v)
            return v

        # 3) 通用 Kismet 原语。字节码里只给了**函数名**，没给库名 ——
        #    所以按名字在各库里找唯一匹配；重名会明确报出来而不是随便挑一个。
        hits = [k for k in _NATIVE_BY_NAME.get(name, ())]
        if len(hits) == 1:
            v = native_call(hits[0], args)
            # ★ 出参也要回写（和 cardnatives 那条路一样）：`Array_Get(arr, i, &Item)`
            #   的第三个参数是调用者的变量，引擎把结果写在那里。
            n = native_arity(hits[0])
            if n is not None and len(e.kids) > n:
                # ★ 这里**不要**按名字去对象的类上找 UFunction：通用原语是
                #   `KismetArrayLibrary` 之类的**库静态函数**，不在对象的类上。
                #   硬找会找到同名的别的东西，把参数整体错位（实测 `Array_Get`
                #   当场变成 "can only concatenate str"）。库函数用 arity 切就够了。
                _ins, outs = self._split_parms(fn, args, e, n)
                self._write_outs(outs, f, v)
            return v
        if len(hits) > 1:
            raise Unimplemented("原语名 %s 在多个库里重名：%s" % (name, hits))

        # 4) 有字节码的蓝图函数 → 递归
        if isinstance(fn, int) and fn:
            return self.call_bytecode(fn, f, obj, args, e)

        # 5) **虚调用**：`EX_VirtualFunction` / `EX_LocalVirtualFunction` 在字节码里
        #    只给**函数名**，不给 UFunction 指针 ⇒ 按名字在**对象自己的类**上找
        #    （`find_function` 会顺着 SuperStruct 往上走，覆写常在父类）。
        #    没有这一步，凡是虚调用都会停在"函数 X"上 —— `CanAttack` 里
        #    `GetLogic()->IsThereGameplayRestriction()` 就是这么停下的。
        target = obj if obj is not None else f.self_obj
        if isinstance(name, str) and target:
            uc = self.s.m.ptr_or_zero(target + 0x10)     # UObject::ClassPrivate
            uf = kismet.find_function(self.s, uc, name) if uc else None
            if uf:
                return self.call_bytecode(uf, f, target, args, e)
        raise Unimplemented("函数 %s" % name)

    def _split_parms(self, fn, args, e, fallback_arity, obj=None, name=None):
        """把调用点上的参数分成 (入参值列表, 出参 kid 列表)。

        优先用 `UFunction` 声明的 `CPF_OutParm` 标志逐个分 —— 这对
        「出参在前面」的函数（如 `CanBeTargetted`）也是对的。

        ★ 字节码里的**虚调用**只给函数名（`EX_VirtualFunction`），`fn` 是个字符串 ⇒
          还要按名字在对象的类上把 `UFunction` 找出来，否则就会退回
          「前 n 个是入参」那个启发式 —— 对 `CanBeTargetted` 那种出参在前的函数
          会整体错位：实测把 `canIt=True` 写进了 `reasonParam1`，
          于是调用方以为"不能指向"，走进了 `Label_2114` 那条拒绝分支。
        """
        if isinstance(fn, str) and obj:
            uc = self.s.m.ptr_or_zero(obj + 0x10)
            fn = (kismet.find_function(self.s, uc, name or fn) if uc else None) or fn
        params = self.params_of(fn) if isinstance(fn, int) and fn else None
        if params and len(params) >= len(e.kids):
            ins, outs = [], []
            for i, k in enumerate(e.kids):
                if params[i][1]:                    # is_out（三元组的第二项）
                    outs.append(k)
                else:
                    ins.append(args[i])
            return ins, outs
        n = fallback_arity if fallback_arity is not None else len(args)
        return args[:n], list(e.kids[n:])

    def _write_outs(self, kids, f, value):
        """把原语的返回值写回调用点上的出参变量。

        ★ 原语返回 **tuple** 时按位置**逐个**写回（`CanBeTargetted` 有四个出参）；
          返回单值时写回最后一个变量引用 —— 单出参的签名一律把它放末尾
          （`IsSameSideUnit(ESideEnum SideToCheck, bool* isIt)`）。
        """
        refs = [k for k in kids if k.op in ("LocalOutVariable", "LocalVariable")]
        if not refs:
            return
        if isinstance(value, tuple):
            for k, v in zip(refs, value):
                f.set(_Ref("out" if k.op == "LocalOutVariable" else "local",
                           k.args["prop"]), v)
            return
        k = refs[-1]
        f.set(_Ref("out" if k.op == "LocalOutVariable" else "local",
                   k.args["prop"]), value)

    _PARM, _OUTPARM, _RETPARM = 0x80, 0x100, 0x400

    def params_of(self, ufunc):
        """`UFunction` 的形参 → [(名字, 是不是出参)]，**按声明顺序**。

        出参靠 `CPF_OutParm(0x100)` 认；返回值 `CPF_ReturnParm(0x400)` 也算出参。
        ★ 顺序来自 `ChildProperties` 链，和字节码里实参的顺序一致 ——
          这正是"按位置对应"能成立的依据。
        """
        if ufunc in self._params:
            return self._params[ufunc]
        from . import props
        out = []
        for pr in props.struct_props(self.s, ufunc):
            fl = pr.get("flags") or 0
            if not (fl & self._PARM):
                continue
            out.append((pr.get("name"), bool(fl & (self._OUTPARM | self._RETPARM)),
                        pr.get("type")))
        self._params[ufunc] = out
        return out

    # 属性类型 → **零值**。引擎里局部变量和出参是零初始化的；我们的影子堆里
    # 没被赋值的变量是 `None`。差别会一路传染：`FString` 的零值是 `""` 而不是 None，
    # 调用方拿 None 去比较/拼接就会走错分支或炸掉。
    _ZERO = {"StrProperty": "", "TextProperty": "", "NameProperty": "",
             "BoolProperty": False, "IntProperty": 0, "Int64Property": 0,
             "ByteProperty": 0, "EnumProperty": 0, "FloatProperty": 0.0,
             "DoubleProperty": 0.0, "ObjectProperty": 0, "ClassProperty": 0,
             "ArrayProperty": (), "StructProperty": None}

    @classmethod
    def zero_of(cls, ptype):
        v = cls._ZERO.get(ptype)
        return list(v) if isinstance(v, tuple) else v

    def call_bytecode(self, ufunc, f, obj, args, e):
        if self.depth > 24:
            raise Unsupported("调用深度超过 24（多半是虚调用解析成了自己）")
        # ★ 同一个 UFunction 在调用栈上再次出现 ⇒ 直接判环，别等深度耗尽。
        #   按名字解析虚调用时很容易解析回自己（覆写链上找到的就是当前这个）。
        if ufunc in self._stack:
            raise Unsupported("调用成环：%s 又调回了自己"
                              % self.resolve.obj(ufunc, "func"))
        code = kismet.script_of(self.s, ufunc)
        if code is None:
            raise Unimplemented("原生函数 %s（没有字节码）" % self.resolve.obj(ufunc, "func"))
        self.depth += 1
        self._stack.add(ufunc)
        try:
            sub = VM(self.s, self.get_field, self.cn, self.hooks)
            sub.depth = self.depth
            sub._stack = self._stack
            # ★ 形参与实参**按位置**对应，不能按名字。
            #   调用点上的变量叫 `CallFunc_CanSelectAsTarget_canIt`，
            #   被调函数里的形参叫 `canIt` —— 名字对不上。先前就是按名字回写的，
            #   于是出参**一个都没写回来**：`CanAttack` 里 `failReason` 始终是 None，
            #   看起来"跑到头了"其实是拿了个空值（2026-09-23 实机发现）。
            params = self.params_of(ufunc)
            names = [k.args.get("prop") for k in e.kids]
            callee_args = {}
            for (pname, _is_out, _pt), v in zip(params, args):
                if pname:
                    callee_args[pname] = v
            r = sub.run(ufunc, self_obj=obj, args=callee_args)
            self.approx |= set(r.get("approx") or ())
            if r["stopped"]:
                # ★ 必须**截断**。停止原因是逐层往上包的（"被调函数停在：…"），
                #   而字符串里的引号每包一层就被转义一次 ⇒ 长度指数级膨胀。
                #   实测递归到深处时这条消息涨到 **32MB**。最里层那句才有用。
                inner = r["stopped"]
                raise Unimplemented("被调函数停在：%s" % (
                    inner if len(inner) <= 300 else inner[:300] + "…（已截断）"))
            out = r["out"]
            ret = None
            for i, (pname, is_out, ptype) in enumerate(params):
                if pname == "ReturnValue":
                    ret = out.get(pname, r["locals"].get(pname))
                    continue
                if not is_out or i >= len(names) or not names[i]:
                    continue
                if pname in out:
                    v = out[pname]
                elif pname in r["locals"]:
                    v = r["locals"][pname]
                else:
                    # ★ 被调函数没给这个出参赋值 ⇒ 引擎那边它是**零值**，不是 None。
                    #   （`FString` 的零值是 `""`。拿 None 回去会让调用方走错分支。）
                    v = self.zero_of(ptype)
                if v is None and ptype:
                    v = self.zero_of(ptype)
                f.locals[names[i]] = v
            if ret is None:
                ret = out.get("ReturnValue")
            return ret
        finally:
            self.depth -= 1
            self._stack.discard(ufunc)


def _build_name_index():
    """函数名 → ["库::函数", …]。字节码里的调用只给函数名，得反查库名。"""
    from . import kismetlib as KL
    idx = {}
    for full in list(KL.PURE) + list(KL.IMPURE):
        idx.setdefault(full.split("::")[-1], []).append(full)
    return idx


_NATIVE_BY_NAME = _build_name_index()
