"""`play_guard` 的组合判据离线测试（假 sess，不碰游戏）。

2026-10-02 三次修正：牌组页也会 `match_active=True`（残留 GameState，kredits={0,1}）⇒ 必须
结合旁证（history 可读 / turn>0 / 场上有牌）才能判"在对局中"。保守方向 = 拦。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from player import play_guard as pg                             # noqa: E402

fails = 0


def chk(name, ok, extra=""):
    global fails
    print("  [%s] %s %s" % ("PASS" if ok else "FAIL", name, extra))
    if not ok:
        fails += 1


class _GS:
    def __init__(self, ma):
        self.match_active = ma


class _KM:
    def __init__(self, ma):
        self._ma = ma

    def gs(self):
        return _GS(self._ma)


class _Snap:
    def __init__(self, turn=0, cards=0):
        self.turn, self.cards = turn, [0] * cards


class _Sess:
    def __init__(self, ma, hist_ok=False, turn=0, cards=0, boom=None, last="ActionPlayCard"):
        self._ma, self._hist_ok, self._turn, self._cards, self._boom = ma, hist_ok, turn, cards, boom
        self._last = last

    def _kardsmem(self):
        return _KM(self._ma)

    def history(self, tail=1):
        if self._boom == "hist":
            raise RuntimeError("history 炸了")
        return {"ok": self._hist_ok, "rows": [{"action_type": self._last}]}

    def snapshot(self):
        if self._boom == "snap":
            raise RuntimeError("snapshot 炸了")
        return _Snap(self._turn, self._cards)


def main():
    chk("主菜单（match_active=False）⇒ 不在对局", pg.in_match(_Sess(False)) is False)
    chk("牌组页：match_active=True 但 history 不可读、turn=0、0 张牌 ⇒ 不在对局",
        pg.in_match(_Sess(True, hist_ok=False, turn=0, cards=0)) is False)
    chk("对局中：history 读得到 ⇒ 在对局", pg.in_match(_Sess(True, hist_ok=True)) is True)
    chk("动作流以 ActionEndMatch 结尾（上一局残留读数）⇒ 不在对局",
        pg.in_match(_Sess(True, hist_ok=True, turn=21, cards=86, last="ActionEndMatch")) is False)
    chk("对局中：history 读不到但场上有牌 ⇒ 在对局",
        pg.in_match(_Sess(True, hist_ok=False, turn=0, cards=3)) is True)
    chk("对局中：turn>0 ⇒ 在对局", pg.in_match(_Sess(True, hist_ok=False, turn=4)) is True)
    chk("history 抛异常 ⇒ 保守判'在对局'（拦）", pg.in_match(_Sess(True, boom="hist")) is True)
    chk("snapshot 抛异常 ⇒ 保守判'在对局'（拦）", pg.in_match(_Sess(True, boom="snap")) is True)
    chk("gs 读不到 ⇒ None（闸门也拦）", pg.in_match(object()) is None)

    ok, im, why = pg.safe_to_start(_Sess(True, hist_ok=False, turn=0, cards=0))
    chk("牌组页 + 有牌组 ⇒ 放行", ok is True and im is False and why == "", str((ok, im, why)))
    ok2, im2, why2 = pg.safe_to_start(_Sess(True, hist_ok=False, turn=0, cards=0), decks=[])
    chk("牌组页但牌组列表空 ⇒ 拦", ok2 is False and "牌组" in why2, why2)
    ok3, im3, why3 = pg.safe_to_start(_Sess(True, hist_ok=True))
    chk("对局中 ⇒ 拦（修复前那个 0xC0000005 事故的闸门）", ok3 is False and why3 == "对局进行中", why3)

    print("\n结论：%s（%d 项失败）" % ("PASS" if not fails else "FAIL", fails))
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
