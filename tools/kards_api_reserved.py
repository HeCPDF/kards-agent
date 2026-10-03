"""实时问游戏官方 API：这些卡现在是不是"进预备"。

端点与查询格式来自 `OCR-Kards-Auto/src/archive/fetch_kards_card.py`（只读参考，不改那个工程）：
    https://herokuapi.kards.com/graphql   —— cards(language, first, offset)
预备名单是**轮换**的，所以必须实时拉，不能用任何缓存快照。

用法：python kards_api_reserved.py            # 拉全量 → 缓存 kards-data/api/kards_api_cards.json → 逐个对
      python kards_api_reserved.py --refresh  # 强制重新拉
"""
import io
import json
import os
import sys
import time
import urllib.request

import _bootstrap  # noqa: F401,E402
from base import paths as P  # noqa: E402

CACHE = P.API_CACHE
API = "https://herokuapi.kards.com/graphql"
HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "origin": "https://www.kards.com",
    "referer": "https://www.kards.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}
QUERY = """query getCards($language: String, $offset: Int) {
  cards(language: $language, first: 200, offset: $offset) {
    pageInfo { count hasNextPage __typename }
    edges { node { cardId reserved json __typename } __typename }
    __typename
  }
}"""


def fetch_all(lang="en"):
    out, off = [], 0
    while True:
        payload = {"operationName": "getCards",
                   "variables": {"language": lang, "offset": off},
                   "query": QUERY}
        req = urllib.request.Request(API, data=json.dumps(payload).encode(),
                                     headers=HEADERS, method="POST")
        with urllib.request.urlopen(req, timeout=40) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        c = (d.get("data") or {}).get("cards") or {}
        edges = c.get("edges") or []
        out += [e["node"] for e in edges]
        pi = c.get("pageInfo") or {}
        print("  offset=%-5d +%d  (共 %s)" % (off, len(edges), pi.get("count")))
        if not pi.get("hasNextPage") or not edges:
            break
        off += len(edges)
        time.sleep(0.25)
    return out


def load(refresh=False):
    if not refresh and os.path.exists(CACHE):
        return json.load(io.open(CACHE, encoding="utf-8"))
    cards = fetch_all()
    json.dump(cards, io.open(CACHE, "w", encoding="utf-8"), ensure_ascii=False)
    return cards


if __name__ == "__main__":
    cards = load("--refresh" in sys.argv)
    print("API 卡牌 %d 张，其中 reserved=True 的 %d 张"
          % (len(cards), sum(1 for c in cards if str(c.get("reserved")).lower() == "true")))
