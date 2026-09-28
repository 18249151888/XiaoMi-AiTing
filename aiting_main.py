#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小米爱听（XiaoMi-AiTing）程序入口。

替代上游 xiaomusic.py，在启动前注入「小米爱听」的增强路由。
"""
import os
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CONF_DIR = os.environ.get("AITING_CONF", "/app/conf")

BANNER = r"""
   __  __ _                     _    _    _ _   _
  \ \/ /(_) __ _  ___  _ __ ___ (_)  / \  (_) |_(_)_ __   __ _
   \  / | |/ _` |/ _ \| '_ ` _ \| | / _ \ | | __| | '_ \ / _` |
   /  \ | | (_| | (_) | | | | | | |/ ___ \| | |_| | | | | (_| |
  /_/\_\|_|\__,_|\___/|_| |_| |_|_/_/   \_\_|\__|_|_| |_|\__, |
                                                         |___/
       小米爱听 XiaoMi-AiTing · 小爱音箱本地音乐 + 网络收音机
"""


def main():
    print(BANNER)

    from xiaomusic.aiting import boot

    boot.ensure_default_config(CONF_DIR)
    ok = boot.install()
    if not ok:
        print("[小米爱听] 警告：增强模块未加载，将以原版模式运行", file=sys.stderr)

    from xiaomusic.cli import main as upstream_main
    upstream_main()


if __name__ == "__main__":
    main()
