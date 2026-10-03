// ★ 2026-09-28：`patch_exe.py` 产出的私服补丁版故意不用正名（另存
// `<stem>.patched.exe`，见 `game-installs/README.md` §5），进程里的主模块
// 名因此真的是 "kards-Win64-Shipping.patched.exe"——`getModuleByName` 精确
// 匹配会直接抛异常，脚本整个装载失败（Python 侧看到的是 rpc.exports 里
// 随便哪个方法都 "unable to find method"，很容易误判成别的故障）。跟
// `board_api._name_matches()` 同一条策略：只换了两个等长字面量，
// SizeOfImage 没变 ⇒ 偏移照样有效，放行 `.patched.exe` 不是新开的口子。
function _findMod() {
    try { return Process.getModuleByName("%(module)s"); } catch (e) {}
    const stem = "%(stem)s";
    const patched = stem + ".patched.exe";
    for (const m of Process.enumerateModules()) {
        const nm = m.name.toLowerCase();
        if (nm === patched) return m;
    }
    throw new Error("module not found: %(module)s (or " + patched + ")");
}
const mod = _findMod();
const peAddr = mod.base.add(ptr(%(pe_rva)d));
const pe = new NativeFunction(peAddr, 'void', ['pointer', 'pointer', 'pointer']);
const PE = %(pe_rva)d;

function P(v) { return ptr(v); }

/* 临时保住 frida 侧 alloc 出来的小缓冲（数组 dataPtr 等），防 GC 提前回收 */
const KEEP = [];

function callPE(obj, func, parms) {
    pe(P(obj), P(func), parms);
    return parms;
}

/* 通用 + 可选的**真实 TArray<UObject*>**：把 dataPtr/num/max 写进 parms 的 arrOff 处。
   ★ 为什么需要它：`cardsCheckFunctions::CanAttack` 的 `cardsInAttackedLocation`
     是**引用参数**（TArray<UBaseCardObject*>&），"防守方那一行有哪些卡"要靠调用方组
     （战斗机拦截就靠它）。传空数组时，攻击者在**前线**的那些组合会让游戏崩/卡。
   dataPtr 必须在**目标进程**里，所以数组也在这里 alloc（并临时留个引用防 GC）。
   ★ `arrOff < 0` = **这次调用没有引用数组参数**，一个字节都不要碰 parms ——
     写侧必须显式说"没有"，否则会在 `parms[0..15]` 上盖掉真实入参
     （踩过：`CanMoveCardToLocation` 的 `Location@0x00` 被数组头写成了 0）。
     越界也在这里挡住：`arrOff+16 > size` 直接抛，绝不写出 buffer 之外。 */
function callRawArrImpl(obj, func, hexIn, arrOff, arrPtrs) {
    const bytes = [];
    for (let i = 0; i < hexIn.length; i += 2) bytes.push(parseInt(hexIn.substr(i, 2), 16));
    const size = Math.max(bytes.length, 1);
    const p = Memory.alloc(size);
    if (bytes.length) p.writeByteArray(bytes);
    const n = (arrPtrs || []).length;
    if (arrOff !== null && arrOff !== undefined && arrOff >= 0) {
        if (arrOff + 16 > size) throw new Error('arrOff ' + arrOff + ' 超出 parms(' + size + ')');
        let data = NULL;
        if (n > 0) {
            data = Memory.alloc(n * 8);
            for (let i = 0; i < n; i++) data.add(i * 8).writePointer(P(arrPtrs[i]));
            KEEP.push(data);
            while (KEEP.length > 16) KEEP.shift();
        }
        p.add(arrOff).writePointer(data);
        p.add(arrOff + 8).writeS32(n);
        p.add(arrOff + 12).writeS32(n);
    } else if (n > 0) {
        throw new Error('给了 ' + n + ' 个数组元素却没给 arrOff');
    }
    callPE(obj, func, p);
    return p.readByteArray(size);
}

// ---------------------------------------------------------------------
// ★ 2026-09-25：GObjects 全量扫描搬进 Frida 进程内执行。
// 原因：Python 这边每读一个字段就是一次跨进程 ReadProcessMemory（几十微秒的系统
// 调用开销），12 万+对象、每个至少读 flags+class+类名 3 次 ⇒ 4~10s。Frida 的
// agent 脚本运行在**目标进程自己的地址空间里**，同样的指针解引用没有系统调用，
// 量级从"微秒×N"降到"纳秒×N"。逻辑照搬 kardsmem/objects.py（GObjects 分块遍历）
// 和 kardsmem/names.py（FNamePool 的 FName 解析），只是从 Python 挪成 JS。
// ---------------------------------------------------------------------
const GOBJ = mod.base.add(ptr(%(gobjects_rva)d));
const NAMEPOOL = mod.base.add(ptr(%(namepool_rva)d));
const ELEMENTS_PER_CHUNK = 0x10000;
const ITEM_SIZE = 0x18;
const OFF_UOBJ_FLAGS = 0x08;
const OFF_UOBJ_CLASS = 0x10;
const OFF_UOBJ_NAME = 0x18;
const RF_CDO = 0x10;
const POOL_BLOCKS_OFF = 0x10;
const POOL_MAX_BLOCKS = 8192;

const _blockCache = {};
function poolBlock(idx) {
    if (idx < 0 || idx >= POOL_MAX_BLOCKS) return null;
    if (_blockCache[idx] !== undefined) return _blockCache[idx];
    let p;
    try { p = NAMEPOOL.add(POOL_BLOCKS_OFF + idx * 8).readPointer(); }
    catch (e) { return null; }
    if (p.isNull()) return null;
    _blockCache[idx] = p;
    return p;
}

function nameOfEntry(a, depth) {
    if (a === null || a.isNull()) return null;
    if (depth === undefined) depth = 0;
    let head;
    try { head = a.readU16(); } catch (e) { return null; }
    const len = head >>> 6;
    if (len === 0) {
        if (depth >= 4) return null;
        let nxt;
        try { nxt = a.add(2).readS32(); } catch (e) { return null; }
        if (nxt === null || nxt < 0) return null;
        return nameAt(nxt, depth + 1);
    }
    if (len > 0x400) return null;
    const wide = (head & 1) !== 0;
    try {
        return wide ? a.add(2).readUtf16String(len) : a.add(2).readCString(len);
    } catch (e) { return null; }
}

function nameAt(idx, depth) {
    if (idx === null || idx < 0) return null;
    const b = poolBlock(idx >>> 16);
    if (b === null) return null;
    return nameOfEntry(b.add((idx & 0xFFFF) * 2), depth);
}

function fnameOf(objPtr, off) {
    if (off === undefined) off = OFF_UOBJ_NAME;
    let idx, number;
    try {
        idx = objPtr.add(off).readS32();
        number = objPtr.add(off + 4).readS32();
    } catch (e) { return null; }
    if (idx < 0) return null;
    const s = nameAt(idx, 0);
    if (s === null) return null;
    return (number > 0) ? (s + "_" + (number - 1)) : s;
}

/* 一次遍历 GObjects，归类出若干目标类名各自的活实例指针（十六进制字符串）。
   等价于 kardsmem 里 `scan_classes()`，但整条循环跑在目标进程里。 */
function scanClassesNative(wantNames, skipCdo) {
    const want = {};
    const out = {};
    for (let i = 0; i < wantNames.length; i++) { want[wantNames[i]] = true; out[wantNames[i]] = []; }
    let numElements, numChunks, chunksPtr;
    try {
        chunksPtr = GOBJ.readPointer();
        numElements = GOBJ.add(0x14).readS32();
        numChunks = GOBJ.add(0x1C).readS32();
    } catch (e) { return {error: "GObjects 头读不出来: " + e.message}; }
    for (let ci = 0; ci < numChunks; ci++) {
        let c;
        try { c = chunksPtr.add(ci * 8).readPointer(); } catch (e) { continue; }
        if (c.isNull()) continue;
        const lo = ci * ELEMENTS_PER_CHUNK;
        const cnt = Math.min(ELEMENTS_PER_CHUNK, numElements - lo);
        if (cnt <= 0) break;
        let buf;
        try { buf = c.readByteArray(cnt * ITEM_SIZE); } catch (e) { continue; }
        const view = new DataView(buf);
        for (let k = 0; k < cnt; k++) {
            const off = k * ITEM_SIZE;
            const lo32 = view.getUint32(off, true);
            const hi32 = view.getUint32(off + 4, true);
            if (lo32 === 0 && hi32 === 0) continue;
            const p = ptr(hi32).shl(32).or(lo32);
            if (skipCdo) {
                let flags;
                try { flags = p.add(OFF_UOBJ_FLAGS).readU32(); } catch (e) { continue; }
                if (flags & RF_CDO) continue;
            }
            let cls;
            try { cls = p.add(OFF_UOBJ_CLASS).readPointer(); } catch (e) { continue; }
            if (cls.isNull()) continue;
            const cname = fnameOf(cls, OFF_UOBJ_NAME);
            if (cname !== null && want[cname]) out[cname].push(p.toString());
        }
    }
    return out;
}

rpc.exports = {
    scanClasses: function (wantNames, skipCdo) {
        return scanClassesNative(wantNames, skipCdo !== false);
    },
    base: function () { return mod.base.toString(); },
    pe: function () { return peAddr.toString(); },

    call0: function (obj, func) {
        callPE(obj, func, NULL);
        return true;
    },
    /* 单指针入参；返回首个字节（用于 bool& success 这类出参） */
    callPtr: function (obj, func, arg) {
        const p = Memory.alloc(8);
        p.writePointer(P(arg));
        callPE(obj, func, p);
        return p.readU8();
    },
    /* 单个标量入参（int32/枚举），ParmsSize=4 */
    callI32: function (obj, func, v) {
        const p = Memory.alloc(4);
        p.writeS32(v);
        callPE(obj, func, p);
        return p.readU8();
    },
    /* 批量：items = [[objHex, funcHex], ...]，每个函数只有一个 bool 出参（getHas* 那类），
       一次 RPC 跑完，返回每个的出参字节（出错记 -1）。用来把"逐牌逐关键词各一次 RPC"合成一次。 */
    callOutU8Batch: function (items) {
        const out = [];
        for (let i = 0; i < items.length; i++) {
            const p = Memory.alloc(1);
            p.writeU8(0);
            try { callPE(items[i][0], items[i][1], p); out.push(p.readU8()); }
            catch (e) { out.push(-1); }
        }
        return out;
    },
    /* 通用：按字节吃入参、返回出参十六进制（够用且不怕对齐） */
    callRaw: function (obj, func, hexIn) {
        const bytes = [];
        for (let i = 0; i < hexIn.length; i += 2) bytes.push(parseInt(hexIn.substr(i, 2), 16));
        const size = Math.max(bytes.length, 1);
        const p = Memory.alloc(size);
        if (bytes.length) p.writeByteArray(bytes);
        callPE(obj, func, p);
        return p.readByteArray(size);
    },

    /* 通用：按 [[objHex,off,kind,value],...] 写若干字段（回读一遍），然后调用一个函数。
       全部在同一次 JS 执行里 —— 用来跑组合实验，避开引擎帧把值清掉的竞态。 */
    writeAndCall: function (writes, callObj, callFunc) {
        const back = [];
        for (let i = 0; i < writes.length; i++) {
            const w = writes[i];
            const a = P(w[0]).add(w[1]);
            if (w[2] === 's32') a.writeS32(w[3] | 0);
            else if (w[2] === 'u8') a.writeU8(w[3] & 0xff);
            else if (w[2] === 'u32') a.writeU32(w[3] >>> 0);
            else if (w[2] === 'ptr') a.writePointer(P(w[3]));
            /* f64：给 `BP_BoardCard_C::targetArrowFinalLength`（double）用 ——
               真鼠标拖箭头会算出长度，我们不动鼠标 ⇒ 落地前必须自己写。 */
            else if (w[2] === 'f64') a.writeDouble(w[3]);
            else throw new Error('bad kind ' + w[2]);
            if (w[2] === 'ptr') back.push(a.readPointer().toString());
            else if (w[2] === 's32') back.push(a.readS32());
            else if (w[2] === 'f64') back.push(a.readDouble());
            else back.push(a.readU8());
        }
        if (callObj !== null && callObj !== undefined && callObj !== '') {
            callPE(callObj, callFunc, NULL);
        }
        return back;
    },
    poke: function (obj, off, kind, value) {
        const a = P(obj).add(off);
        if (kind === 'u8') a.writeU8(value & 0xff);
        else if (kind === 'u32') a.writeU32(value >>> 0);
        else if (kind === 's32') a.writeS32(value | 0);
        else if (kind === 'f32') a.writeFloat(value);
        else if (kind === 'ptr') a.writePointer(P(value));
        else throw new Error('bad kind ' + kind);
        return true;
    },
    /* 写一个字段 + 立刻调用一个函数，**在同一次 JS 执行里**完成。
       ★ 用途：箭头每帧会把它自己按"真实鼠标位置"算出来的 overCardID 重算一遍，
         没有真实鼠标时那个值就是 0 —— 分两次 RPC 调用（写、再调用）中间会插进
         引擎帧，写进去的值当场被清掉。实测：写 61 → 0.5s 后读到 0。
         把"写 + 落地"塞进一次调用，窗口缩到微秒级。 */
    pokeAndCall0: function (obj, off, kind, value, callObj, callFunc) {
        const a = P(obj).add(off);
        if (kind === 's32') a.writeS32(value | 0);
        else if (kind === 'u8') a.writeU8(value & 0xff);
        else if (kind === 'u32') a.writeU32(value >>> 0);
        else if (kind === 'ptr') a.writePointer(P(value));
        else throw new Error('bad kind ' + kind);
        callPE(callObj, callFunc, NULL);
        return true;
    },
    /* 通用 + 可选的**真实 TArray<UObject*>**：见上面 callRawArrImpl 的注释。
       `arrOff < 0` = 这次调用没有引用数组参数。 */
    callRawArr: function (obj, func, hexIn, arrOff, arrPtrs) {
        return callRawArrImpl(obj, func, hexIn, arrOff, arrPtrs);
    },
    /* ★ 2026-09-25：**先临时写几个字段 → 调用 → 立刻还原**，全在同一次 JS 执行里。
       用途：`CanMoveCardToLocation` 读的是 `PlayerController->SelectedCard`
       （"当前正在拖的那张卡"，`BP_PlayerController_C::SelectedCard // 0x0950`），
       预检阶段没有真实拖拽 ⇒ 它是 None ⇒ `IsValid` 不过 ⇒ **永远**回 False。
       实测：6 个 (卡, 地点) 组合全 0，而同一次读 `SelectedCard` 就是 None。
       把"假装正在拖这张卡"的那个字段临时写上再问，问题才从"拖拽中间态"
       变成"这张卡能不能放进这行"。
       ★ **必须还原**：`SelectedCard` 影响选中高亮（可见效果），不能留在那儿；
         顺带把 cursor 那几个字段也还原掉（原来 `can_move_to` 会留在原地）。
       `writes` 与 `writeAndCall` 同格式（这个那个只写不还原）。 */
    callRawArrHold: function (writes, obj, func, hexIn, arrOff, arrPtrs) {
        const saved = [];
        for (let i = 0; i < writes.length; i++) {
            const w = writes[i];
            const a = P(w[0]).add(w[1]);
            if (w[2] === 'ptr') { saved.push([a, 'ptr', a.readPointer()]); a.writePointer(P(w[3])); }
            else if (w[2] === 's32') { saved.push([a, 's32', a.readS32()]); a.writeS32(w[3] | 0); }
            else if (w[2] === 'u8') { saved.push([a, 'u8', a.readU8()]); a.writeU8(w[3] & 0xff); }
            else throw new Error('bad kind ' + w[2]);
        }
        let out;
        try {
            out = callRawArrImpl(obj, func, hexIn, arrOff, arrPtrs);
        } finally {
            for (let i = saved.length - 1; i >= 0; i--) {
                const s = saved[i];
                if (s[1] === 'ptr') s[0].writePointer(s[2]);
                else if (s[1] === 's32') s[0].writeS32(s[2]);
                else s[0].writeU8(s[2]);
            }
        }
        return out;
    },
    /* 攻击落地：**写 overCardID → 松手**，全在**同一次 JS 执行**里是必须的
       （分两次 RPC 之间会插进引擎帧，箭头自己的 tick 会按真实鼠标重算几何）。
       ★★ 2026-09-25 更正一条**写错过的结论**（handoff §二4 说 `spectatorArrowNewTarget`
           "会把头部平面搬到目标卡的世界坐标"）：把它的字节码 dump 出来只有 4 条
           （`overCardID = 参数`）——**它根本不碰几何**，箭头头部跟真实鼠标走。
           `BP_Logic_C::GetTargetArrowTargetCard` 也只读 `OutActors[0]->overCardID`
           （1.58 导出 `BP_Logic.cpp:7051`）⇒ 判定只认这个**字段**，不认几何。
           所以"aim"就是"写字段"，没有别的机关；真正缺的那一步是**目标 actor 的悬停**
           （见 Python 侧 `attack_card` 的注释），不在这个函数里。 */
    /* 写若干字段 → 依次调用**两个**函数，全在同一次 JS 执行里。
       用途：`BP_HandCard_C` 上同时有 `OnActorEndDrag`（出牌提交）和 `OnActorMouseUp`
       （看着像清 touched/回盘的收尾）——真实松手很可能**两个都发**（handoff 里
       标记为"还没验证过"的那条），带目标出牌要一次把两跳都复刻出来。 */
    writeAndCall2: function (writes, objA, funcA, objB, funcB) {
        const back = [];
        for (let i = 0; i < writes.length; i++) {
            const w = writes[i];
            const a = P(w[0]).add(w[1]);
            if (w[2] === 's32') a.writeS32(w[3] | 0);
            else if (w[2] === 'u8') a.writeU8(w[3] & 0xff);
            else if (w[2] === 'ptr') a.writePointer(P(w[3]));
            else if (w[2] === 'f64') a.writeDouble(w[3]);
            else throw new Error('bad kind ' + w[2]);
            back.push(w[2] === 'ptr' ? a.readPointer().toString()
                                     : (w[2] === 's32' ? a.readS32()
                                        : (w[2] === 'f64' ? a.readDouble() : a.readU8())));
        }
        if (objA) callPE(objA, funcA, NULL);
        if (objB) callPE(objB, funcB, NULL);
        return back;
    },
    /* 写若干字段 → 依次调用若干函数（**带参/空参混合**），全在**同一次 JS 执行**里。
       `calls` 每项 `[objHex, funcHex, hexIn]`；`hexIn` 空字符串 ⇒ 用 NULL 参数调。
       用途（2026-09-26，攻击落地）——必须一次做完的三件事：
         ① 写每个箭头的 `overCardID`（游戏按 OutActors[0] 读，见 Python 侧注释）
         ② `箭头头部平面->K2_SetRelativeLocation(远点, false, …, false)`（带参）
            —— `GetArrowLength()` = |头部平面 − 箭头原点|²，**真人拖拽会把这个平面拖远**；
               我们不动真实鼠标 ⇒ 它只有一百多 ⇒ `IsTargetArrowLengthValid()`（>3000）不过
               ⇒ `BP_BoardCard::OnActorMouseUp` 里的提交被跳过（动作流零新增）。
         ③ 攻击者 actor 上 `OnActorMouseUp`（空参）+ 可选 `OnActorEndDrag`
       拆成两次 RPC 中间会插引擎帧：箭头 tick 按真实鼠标把头部平面搬回去 ⇒ 又白做。 */
    writeThenCalls: function (writes, calls) {
        const back = [];
        for (let i = 0; i < writes.length; i++) {
            const w = writes[i];
            const a = P(w[0]).add(w[1]);
            if (w[2] === 's32') a.writeS32(w[3] | 0);
            else if (w[2] === 'u8') a.writeU8(w[3] & 0xff);
            else if (w[2] === 'ptr') a.writePointer(P(w[3]));
            else if (w[2] === 'f64') a.writeDouble(w[3]);
            else throw new Error('bad kind ' + w[2]);
            back.push(w[2] === 'ptr' ? a.readPointer().toString()
                                     : (w[2] === 's32' ? a.readS32()
                                        : (w[2] === 'f64' ? a.readDouble() : a.readU8())));
        }
        for (let i = 0; i < calls.length; i++) {
            const c = calls[i];
            let p = NULL;
            if (c[2]) {
                const bytes = [];
                for (let j = 0; j < c[2].length; j += 2) {
                    bytes.push(parseInt(c[2].substr(j, 2), 16));
                }
                if (bytes.length) {
                    p = Memory.alloc(bytes.length);
                    p.writeByteArray(bytes);
                }
            }
            callPE(P(c[0]), P(c[1]), p);
        }
        return back;
    },
    /* 写若干字段 → **带参调用**一次 → 按 mask 还原，全在同一次 JS 执行里。
       用途（2026-09-25，两阶段第二阶段的"点目标"）：
         `BP_Logic_C::GlobalMouseUp(目标actor, &bWasConsumed)` 会读
         `箭头->overCardID` 与 `卡对象->targetOverride`，而箭头 tick 每帧按**真实鼠标**
         重算它们 ⇒ 写和调用必须同一次执行（分两次 RPC 中间插一帧就被清）。
       `writes` 每项可带第 5 个元素：1 = **不还原**。
         ★ 箭头那几项就要用 1：`GlobalMouseUpBattle` 内部会
           `DestroyAllActorsOfClass(BP_targetArrowRVX_C)`，再去还原已销毁 actor 的字段
           是不必要的风险；而卡对象上的 `targetOverride` 必须还原（那是我们为复刻
           鼠标悬停临时写的，游戏自己也是探测完就清，见 `BP_HandCard.cpp:4795`）。 */
    writeCallRaw: function (writes, obj, func, hexIn, arrOff, arrPtrs) {
        const saved = [];
        for (let i = 0; i < writes.length; i++) {
            const w = writes[i];
            const a = P(w[0]).add(w[1]);
            const keep = w.length > 4 && w[4];
            let old;
            if (w[2] === 'ptr') { old = a.readPointer(); a.writePointer(P(w[3])); }
            else if (w[2] === 's32') { old = a.readS32(); a.writeS32(w[3] | 0); }
            else if (w[2] === 'u8') { old = a.readU8(); a.writeU8(w[3] & 0xff); }
            else throw new Error('bad kind ' + w[2]);
            if (!keep) saved.push([a, w[2], old]);
        }
        let out;
        try {
            out = callRawArrImpl(obj, func, hexIn, arrOff, arrPtrs);
        } finally {
            for (let i = saved.length - 1; i >= 0; i--) {
                const s = saved[i];
                if (s[1] === 'ptr') s[0].writePointer(s[2]);
                else if (s[1] === 's32') s[0].writeS32(s[2]);
                else s[0].writeU8(s[2]);
            }
        }
        return out;
    },
    peek: function (obj, off, kind) {        const a = P(obj).add(off);
        if (kind === 'u8') return a.readU8();
        if (kind === 'u32') return a.readU32();
        if (kind === 's32') return a.readS32();
        if (kind === 'ptr') return a.readPointer().toString();
        throw new Error('bad kind ' + kind);
    },
};

/* =====================================================================================
   游戏线程调度（2026-10-01）
   ★ 为什么：注入调用原来跑在 frida 自己的线程上（callPE 直接 pe(...)），而游戏线程同时在跑。
     转储分析（kards+0x120F1B6，UWorld+0x460 = OnActorSpawned 委托列表）：演员迭代器
     （GetAllActorsOfClass / TActorIterator / DestroyActor …，蓝图里遍地都是）每次迭代都对这个
     无锁的列表做 Add/Remove，两个线程同时做会留下指向已释放实例的悬空项，之后压实时崩溃
     （四次崩溃同一模式）。在非游戏线程执行蓝图还会碰到线程局部状态为空（"access violation
     accessing 0x24"）。
   做法：Interceptor 的回调本来就跑在**被钩函数所在的线程**上。在 ProcessEvent 入口挂一个
     CModule 原生钩子做便宜的判断（有排队任务且当前是游戏线程），命中才回调 JS 把**整个 RPC 函数体**
     原样跑完 —— 所以原有的"先写字段、紧接着调用"的顺序一个字节都没变，只是换了线程。RPC 线程在
     协作式的 Sleep 里等（会放开 JS 锁，让游戏线程上的回调进得来）。
   保险：4 s 内游戏线程没接手就取消并抛错；没装（gtInstall 没调）时行为与以前完全一样。 */
const GT = { on: false, enabled: true, job: null, cm: null, cb: null, task: null, res: null, err: null,
             sleep: null, cancel: null, done: 0 };
function gtInstall(tid) {
    if (GT.on) { GT.job.add(4).writeU32(tid >>> 0); return true; }
    GT.job = Memory.alloc(64);
    for (let i = 0; i < 64; i += 4) GT.job.add(i).writeU32(0);
    GT.job.add(4).writeU32(tid >>> 0);
    GT.cb = new NativeCallback(function () {
        try { GT.res = GT.task(); GT.err = null; }
        catch (e) { GT.res = null; GT.err = String(e && e.stack ? e.stack : e); }
        GT.done++;
    }, 'void', []);
    const src = [
        '#include <gum/guminterceptor.h>',
        'extern volatile unsigned int job[16];',
        'extern void js_run (void);',
        'void on_enter (GumInvocationContext * ic) {',
        '  { unsigned long long tk = *(unsigned long long *) ((char *) job + 40);',
        '    if (tk != 0 && gum_invocation_context_get_thread_id (ic) == job[1] &&',
        '        (unsigned long long) gum_invocation_context_get_nth_argument (ic, 1) == tk) job[12]++; }',
        '  if (job[0] != 1) return;',
        '  if (gum_invocation_context_get_thread_id (ic) != job[1]) return;',
        '  { unsigned long long want = *(unsigned long long *) ((char *) job + 32);',
        '    if (want != 0 && (unsigned long long) gum_invocation_context_get_nth_argument (ic, 1) != want) return; }',
        '  job[0] = 3;',
        '  js_run ();',
        '  job[0] = 2;',
        '}',
    ].join('\n');
    GT.cm = new CModule(src, { job: GT.job, js_run: GT.cb });
    GT.sleep = new NativeFunction(Process.getModuleByName('kernel32.dll').getExportByName('Sleep'),
                                  'void', ['uint32']);
    Interceptor.attach(peAddr, { onEnter: GT.cm.on_enter });
    GT.on = true;
    return true;
}
function runOnGT(fn) {
    if (!GT.on || !GT.enabled) return fn();
    GT.task = fn; GT.res = null; GT.err = null;
    GT.job.writeU32(1);
    const t0 = Date.now();
    for (;;) {
        const st = GT.job.readU32();
        if (st === 2) break;
        const dt = Date.now() - t0;
        if (st === 1 && dt > 4000) {          // 没人接手：撤回（游戏线程只做 1→3，这里只做 1→0）
            GT.job.writeU32(0);
            GT.task = null;
            throw new Error('gt-dispatch: game thread did not pick up the job within 4 s (no ProcessEvent on it)');
        }
        if (dt > 30000) throw new Error('gt-dispatch: job still running after 30 s');
        GT.sleep(1);
    }
    GT.job.writeU32(0);
    GT.task = null;
    if (GT.err !== null) throw new Error(GT.err);
    return GT.res;
}
rpc.exports.gtInstall = function (tid) { return gtInstall(tid); };
rpc.exports.gtStatus = function () { return { on: GT.on, enabled: GT.enabled, done: GT.done, state: GT.on ? GT.job.readU32() : -1 }; };
/* 只在**指定 UFunction** 的 ProcessEvent 入口执行任务（0 = 任意一次 ProcessEvent）。
   用途：把注入任务放到和真实输入同一个时间点（玩家控制器 tick），而不是任意一次 ProcessEvent
   （可能正落在 Slate 绘制/动画回调里，在那里触发游戏结算逻辑不安全）。 */
rpc.exports.gtSetFilter = function (fnHex) {
    if (!GT.on) return false;
    GT.job.add(32).writePointer(fnHex ? ptr(fnHex) : NULL);
    return true;
};
/* 运行时开关（A/B 对照用）：false ⇒ 调用退回在 frida 线程上直接跑（旧行为）；钩子本身保留。 */
/* 帧计数：玩家控制器 ReceiveTick 在游戏线程上每进一次 +1（job[12]）。模拟输入的"停留"
   按这个算帧数，而不是只按墙钟——窗口失焦时 UE 会降帧，固定 0.5 s 里可能一帧都没跑。 */
rpc.exports.gtSetTickFn = function (fnHex) {
    if (!GT.on) return false;
    GT.job.add(40).writePointer(fnHex ? ptr(fnHex) : NULL);
    return true;
};
rpc.exports.gtTicks = function () { return GT.on ? GT.job.add(48).readU32() : -1; };
rpc.exports.gtSetOn = function (on) { GT.enabled = !!on; return GT.enabled; };
rpc.exports.gtProbe = function () { return runOnGT(function () { return Process.getCurrentThreadId(); }); };
rpc.exports.gtMyTid = function () { return Process.getCurrentThreadId(); };
/* 凡是函数体里用到 callPE / pe( 的导出，一律包一层"在游戏线程上执行"；纯读内存的不动。 */
(function () {
    const skip = { gtInstall: 1, gtStatus: 1, gtProbe: 1, gtMyTid: 1, gtSetOn: 1, gtSetFilter: 1, gtSetTickFn: 1, gtTicks: 1 };
    for (const k of Object.keys(rpc.exports)) {
        const f = rpc.exports[k];
        if (typeof f !== 'function' || skip[k]) continue;
        if (!/callPE|callRawArrImpl|\bpe\(/.test(f.toString())) continue;
        rpc.exports[k] = function () { const a = arguments; return runOnGT(function () { return f.apply(null, a); }); };
    }
})();
