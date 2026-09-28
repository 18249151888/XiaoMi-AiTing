#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""JSON 文件持久化：带原子写入与线程锁，避免并发写坏文件。"""
import json
import os
import tempfile
import threading


class JsonStore:
    """线程安全的 JSON 文件读写。"""

    def __init__(self, path, default=None):
        self.path = path
        self.default = default if default is not None else {}
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)

    def load(self):
        with self._lock:
            if not os.path.exists(self.path):
                return json.loads(json.dumps(self.default, ensure_ascii=False))
            try:
                with open(self.path, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                # 文件损坏时备份并回退默认值
                try:
                    os.replace(self.path, self.path + ".broken")
                except Exception:
                    pass
                return json.loads(json.dumps(self.default, ensure_ascii=False))

    def save(self, data):
        with self._lock:
            d = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
