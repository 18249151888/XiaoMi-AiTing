#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""电台服务：内置分类电台库 + 用户自定义/收藏 + 连通性检查 + 在线更新。

数据分两层：
  - 内置库  app/data/radio_library.json   （随程序发布，只读）
  - 用户库  <conf>/radio_user.json        （收藏、自定义、播放计数，可写）
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

RB_MIRRORS = [
    "https://de1.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
    "https://at1.api.radio-browser.info",
    "https://fi1.api.radio-browser.info",
]

BOARD_ORDER = ["大陆", "港澳台", "海外华语"]
BOARD_ICON = {"大陆": "🇨🇳", "港澳台": "🏙️", "海外华语": "🌏"}


def station_id(url):
    """由播放地址生成稳定 ID"""
    return hashlib.md5(url.strip().encode("utf-8")).hexdigest()[:12]


def _clean(s, n=80):
    return re.sub(r"\s+", " ", (s or "").strip())[:n]


class RadioService:
    """电台库服务"""

    def __init__(self, library_path, user_store, log=None):
        self.library_path = library_path
        self.user_store = user_store          # core.store.JsonStore
        self.log = log
        self._lock = threading.RLock()
        self._builtin = []                     # 内置电台
        self._index = {}                       # id -> station
        self._meta = {}
        self._user = {"favorites": [], "custom": [], "recent": [], "counts": {}}
        self.load()

    # ------------------------------------------------------------ 加载

    def load(self):
        with self._lock:
            self._builtin = []
            if os.path.exists(self.library_path):
                try:
                    with open(self.library_path, encoding="utf-8") as f:
                        data = json.load(f)
                    self._builtin = data.get("stations", [])
                    self._meta = {
                        "version": data.get("version", 1),
                        "source": data.get("source", ""),
                        "total": len(self._builtin),
                    }
                except Exception as e:
                    if self.log:
                        self.log.error(f"加载内置电台库失败: {e}")
            self._user = self.user_store.load() or {}
            self._user.setdefault("favorites", [])
            self._user.setdefault("custom", [])
            self._user.setdefault("recent", [])
            self._user.setdefault("counts", {})

            self._index = {}
            for st in self._builtin:
                st = dict(st)
                st.setdefault("id", station_id(st.get("url", "")))
                st["source"] = "builtin"
                self._index[st["id"]] = st
            for st in self._user["custom"]:
                st = dict(st)
                st.setdefault("id", station_id(st.get("url", "")))
                st["source"] = "custom"
                self._index[st["id"]] = st
            self._mark_favorites()
            if self.log:
                self.log.info(
                    f"电台库加载完成：内置 {len(self._builtin)} 个，"
                    f"自定义 {len(self._user['custom'])} 个，"
                    f"收藏 {len(self._user['favorites'])} 个")

    def _mark_favorites(self):
        favs = set(self._user.get("favorites", []))
        for st in self._index.values():
            st["fav"] = st["id"] in favs

    def _save_user(self):
        self.user_store.save(self._user)

    # ------------------------------------------------------------ 设置

    def get_settings(self):
        """电台相关设置。normalize=响度均衡（默认开启）。"""
        s = self._user.setdefault("settings", {})
        s.setdefault("normalize", True)
        return s

    def set_settings(self, **kw):
        s = self.get_settings()
        for k, v in kw.items():
            if v is not None:
                s[k] = v
        self._save_user()
        return s

    # ------------------------------------------------------------ 查询

    def meta(self):
        return dict(self._meta, custom=len(self._user["custom"]),
                    favorites=len(self._user["favorites"]))

    def all_stations(self):
        return list(self._index.values())

    def get(self, sid):
        return self._index.get(sid)

    def tree(self):
        """分类树：板块 -> 地区 -> [类型]"""
        t = {}
        for st in self._index.values():
            b = st.get("board") or "其他"
            r = st.get("region") or "其他"
            c = st.get("category") or "其他"
            t.setdefault(b, {}).setdefault(r, {})
            t[b][r][c] = t[b][r].get(c, 0) + 1
        out = []
        for b in BOARD_ORDER + [x for x in t if x not in BOARD_ORDER]:
            if b not in t:
                continue
            regions = [{"name": r, "count": sum(v.values()), "categories":
                        [{"name": k, "count": n} for k, n in sorted(v.items(), key=lambda x: -x[1])]}
                       for r, v in sorted(t[b].items(), key=lambda x: -sum(x[1].values()))]
            out.append({"board": b, "icon": BOARD_ICON.get(b, "📻"),
                        "count": sum(x["count"] for x in regions), "regions": regions})
        return out

    def list(self, board=None, region=None, category=None, keyword=None,
             fav_only=False, source=None, page=1, limit=100, sort="hot"):
        res = []
        kw = (keyword or "").strip().lower()
        favs = set(self._user.get("favorites", []))
        counts = self._user.get("counts", {})

        for st in self._index.values():
            if board and st.get("board") != board:
                continue
            if region and st.get("region") != region:
                continue
            if category and st.get("category") != category:
                continue
            if source and st.get("source") != source:
                continue
            if fav_only and st["id"] not in favs:
                continue
            if kw and kw not in (st.get("name", "") + st.get("tags", "")).lower():
                continue
            res.append(st)

        if sort == "hot":
            res.sort(key=lambda x: (-(x.get("votes") or 0), -(x.get("clicks") or 0), x["name"]))
        elif sort == "name":
            res.sort(key=lambda x: x["name"])
        elif sort == "play":
            res.sort(key=lambda x: (-(counts.get(x["id"], 0)), x["name"]))
        elif sort == "recent":
            order = {s: i for i, s in enumerate(self._user.get("recent", []))}
            res.sort(key=lambda x: order.get(x["id"], 10 ** 9))

        total = len(res)
        page = max(1, int(page or 1))
        limit = max(1, min(int(limit or 100), 500))
        start = (page - 1) * limit
        return {"total": total, "page": page, "limit": limit,
                "items": res[start:start + limit]}

    def recent(self, limit=30):
        order = self._user.get("recent", [])
        out = [self._index[s] for s in order if s in self._index]
        return out[:limit]

    # ------------------------------------------------------------ 用户操作

    def toggle_favorite(self, sid):
        with self._lock:
            favs = self._user["favorites"]
            if sid in favs:
                favs.remove(sid)
                state = False
            else:
                if sid not in self._index:
                    return None
                favs.append(sid)
                state = True
            self._save_user()
            if sid in self._index:
                self._index[sid]["fav"] = state
            return state

    def add_custom(self, name, url, board="我的电台", region="自定义",
                   category="自定义", tags=""):
        name, url = _clean(name, 60), (url or "").strip()
        if not name or not url.startswith(("http://", "https://")):
            raise ValueError("名称和合法的播放地址（http/https）必填")
        sid = station_id(url)
        with self._lock:
            for st in self._user["custom"]:
                if st.get("id") == sid or st.get("url") == url:
                    raise ValueError("该电台地址已存在")
            st = {"id": sid, "name": name, "url": url, "board": board,
                  "region": region, "category": category, "tags": tags,
                  "source": "custom", "ok": None, "fav": False,
                  "votes": 0, "clicks": 0}
            self._user["custom"].append(st)
            self._index[sid] = st
            self._save_user()
            return st

    def update_custom(self, sid, **kw):
        with self._lock:
            for st in self._user["custom"]:
                if st["id"] == sid:
                    for k in ("name", "url", "board", "region", "category", "tags"):
                        if k in kw and kw[k] is not None:
                            st[k] = _clean(kw[k], 120) if k != "url" else kw[k].strip()
                    self._index[sid] = st
                    self._save_user()
                    return st
        raise ValueError("未找到该自定义电台")

    def delete_custom(self, sid):
        with self._lock:
            before = len(self._user["custom"])
            self._user["custom"] = [s for s in self._user["custom"] if s["id"] != sid]
            if len(self._user["custom"]) == before:
                raise ValueError("未找到该自定义电台")
            self._user["favorites"] = [f for f in self._user["favorites"] if f != sid]
            self._index.pop(sid, None)
            self._save_user()
            return True

    def mark_played(self, sid):
        with self._lock:
            rec = self._user["recent"]
            if sid in rec:
                rec.remove(sid)
            rec.insert(0, sid)
            del rec[60:]
            counts = self._user["counts"]
            counts[sid] = counts.get(sid, 0) + 1
            self._save_user()

    # ------------------------------------------------------------ 连通性

    def check(self, sid, timeout=8):
        st = self._index.get(sid)
        if not st:
            raise ValueError("电台不存在")
        ok, code, ctype = self._probe(st["url"], timeout)
        with self._lock:
            st["ok"] = ok
            st["checked_at"] = int(time.time())
            for s in self._user["custom"]:
                if s["id"] == sid:
                    s["ok"] = ok
                    self._save_user()
        return {"id": sid, "ok": ok, "status": code, "ctype": ctype}

    @staticmethod
    def _probe(url, timeout=8):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Range": "bytes=0-1023"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                ct = (r.headers.get("Content-Type") or "").lower()
                r.read(512)
                ok = r.status in (200, 206)
                return ok, r.status, ct
        except Exception as e:
            return False, 0, str(e)[:80]

    # ------------------------------------------------------------ 在线更新

    def fetch_online(self, keyword="", country="CN", limit=200):
        """从 radio-browser 拉取电台（供后台批量导入）"""
        params = {"limit": limit, "hidebroken": "true",
                  "order": "clickcount", "reverse": "true"}
        if keyword:
            path = "/json/stations/search"
            params["name"] = keyword
            params["language"] = "chinese"
        else:
            path = "/json/stations/bycountrycodeexact/" + (country or "CN")
        q = "?" + urllib.parse.urlencode(params)
        last = None
        for base in RB_MIRRORS:
            try:
                req = urllib.request.Request(base + path + q, headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=25) as r:
                    return json.loads(r.read().decode("utf-8"))
            except Exception as e:
                last = e
        raise RuntimeError(f"在线电台源不可用: {last}")
