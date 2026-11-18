#!/usr/bin/env python3
"""FxEmbed API v2（api.fxtwitter.com/2/...）でXを読み取り専用で検索する。

fx.py（v1形式：単一投稿・プロフィール取得）の相方。こちらは v2 の
キーワード検索・トレンド・プロフィールRSS を担当する。

使い方:
  fx2.py search "<キーワード>" [--top]   キーワード検索（既定は新着順）
  fx2.py trends                          トレンド一覧
  fx2.py rss <user>                      プロフィールRSS（feed.xml）のURLを表示

ログイン不要・読み取りのみ。非公式APIなので低頻度で使うこと。
出典: docs.fxembed.com（FxEmbed API v2）。2026-11-18 実地テスト済み。
"""
import json
import subprocess
import sys
import time
import urllib.parse

BASE = "https://api.fxtwitter.com"


def fetch(path: str) -> dict:
    # fx.py と同じ作法：urllibだと403になるためcurl経由、最大3回再試行。
    last: dict = {}
    for attempt in range(3):
        out = subprocess.run(
            ["curl", "-s", "--max-time", "20", BASE + path],
            capture_output=True, text=True, check=True,
        )
        last = json.loads(out.stdout)
        if last.get("code") == 200:
            return last
        time.sleep(2)
    return last


def show_search(d: dict) -> None:
    results = d.get("results") or []
    print(f"検索結果: {len(results)}件")
    for r in results:
        if r.get("type") != "status":
            continue
        a = r.get("author") or {}
        print(f"- {r.get('url')}")
        print(f"  {a.get('name')} (@{a.get('screen_name')}) フォロワー{a.get('followers')}")
        text = (r.get("text") or "").replace("\n", " ")
        print(f"  {text[:200]}")
        stats = []
        for key, label in (("likes", "いいね"), ("reposts", "リポスト"),
                           ("replies", "返信"), ("views", "表示")):
            if r.get(key) is not None:
                stats.append(f"{label}{r[key]}")
        if stats:
            print(f"  反応: {' / '.join(stats)}")


def show_trends(d: dict) -> None:
    trends = d.get("trends") or []
    print(f"トレンド: {len(trends)}件")
    for t in trends:
        ctx = t.get("context") or ""
        print(f"- {t.get('name')}" + (f"（{ctx}）" if ctx else ""))


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)
    cmd, rest = args[0], args[1:]
    if cmd == "search":
        feed = "latest"
        words = []
        for a in rest:
            if a == "--top":
                feed = "top"
            else:
                words.append(a)
        if not words:
            print("キーワードを指定してください")
            sys.exit(1)
        q = urllib.parse.quote(" ".join(words))
        show_search(fetch(f"/2/search?q={q}&feed={feed}"))
    elif cmd == "trends":
        show_trends(fetch("/2/trends"))
    elif cmd == "rss":
        if not rest:
            print("ユーザー名を指定してください")
            sys.exit(1)
        user = rest[0].lstrip("@")
        # フィードは api. ではなく fxtwitter.com 側（2026-11-18 実測で確認）
        print(f"https://fxtwitter.com/{user}/feed.xml")
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
