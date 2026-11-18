#!/usr/bin/env python3
"""日本語テクノロジー・ブリーフィング生成（試作v1.2）"""
import json
import os
import shutil
import re
import subprocess
import sys
import unicodedata
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone, timedelta

JST = timezone(timedelta(hours=9))

FEEDS = [
    ("ITmedia", "https://rss.itmedia.co.jp/rss/2.0/news_bursts.xml", None),
    ("ITmedia ENT", "https://rss.itmedia.co.jp/rss/2.0/enterprise.xml", None),
    ("GIGAZINE", "https://gigazine.net/news/rss_2.0/", None),
    ("GN:基本情報", "https://news.google.com/rss/search?q=%E5%9F%BA%E6%9C%AC%E6%83%85%E5%A0%B1%E6%8A%80%E8%A1%93%E8%80%85&hl=ja&gl=JP&ceid=JP:ja", "IT資格"),
    ("GN:応用情報・支援士", "https://news.google.com/rss/search?q=%E5%BF%9C%E7%94%A8%E6%83%85%E5%A0%B1%E6%8A%80%E8%A1%93%E8%80%85%20OR%20%E6%83%85%E5%A0%B1%E5%87%A6%E7%90%86%E5%AE%89%E5%85%A8%E7%A2%BA%E4%BF%9D%E6%94%AF%E6%8F%B4%E5%A3%AB&hl=ja&gl=JP&ceid=JP:ja", "IT資格"),
]
WINDOW_H = {"GN:基本情報": 24 * 7, "GN:応用情報・支援士": 24 * 7}
PR_WORDS = ["発売", "ランキング", "おすすめ", "キャンペーン", "同時発売", "セール"]
OTHER_EXAM_WORDS = ["行政書士", "社労士", "宅建", "司法書士", "税理士", "公認会計士", "中小企業診断士", "ファイナンシャルプランナー", "簿記"]
IT_ANCHORS = ["基本情報", "応用情報", "情報処理", "IPA", "ITパスポート", "支援士", "高度試験"]
NETA_RULES = [
    ("パスワード", "弱いパスワード・使い回しの対処記事に化ける"),
    ("漏えい", "自分の情報が漏れたか確認する手順の記事に化ける"),
    ("障害", "障害のときに利用者がやることチェック記事に化ける"),
    ("不具合", "同じ症状の人向けの切り分け記事に化ける"),
    ("アップデート", "更新してよいか・更新後の不調対処の記事に化ける"),
    ("バッテリー", "電池の減り・劣化チェックの記事に化ける"),
    ("詐欺", "手口と見分け方の注意喚起記事に化ける"),
]

CATEGORIES = [
    ("セキュリティ", ["漏えい", "不正アクセス", "脆弱性", "パスワード", "ランサム", "フィッシング",
                       "セキュリティ", "マルウェア", "情報流出", "サイバー", "侵害", "窃取"]),
    ("AI・Muse", ["AI", "Claude", "GPT", "LLM", "エージェント", "生成AI", "Muse", "人工知能",
                    "機械学習", "モデル"]),
    ("スマホ・PC", ["iPhone", "Android", "スマホ", "Windows", "Mac", "パソコン", "アップデート",
                     "不具合", "携帯", "タブレット", "通信", "ドコモ", "KDDI", "ソフトバンク", "楽天モバイル", "NVIDIA", "GPU"]),
    ("IT資格", ["基本情報", "応用情報", "試験", "資格", "IPA", "情報処理"]),
]
GENERIC_BOOST = ["障害", "復旧", "発表", "開始", "終了", "注意", "警告"]

X_QUERIES = ["Claude Code", "Windows 不具合", "iPhone バッテリー", "パスワード 漏えい", "スマホ 障害"]
SPAM_PATTERNS = ["muse.ai/join", "招待コード", "コードを利用すると", "クーポン", "送料無料", "【PR", "PR楽天", "円OFF"]


def fetch(url):
    r = subprocess.run(["curl", "-s", "--max-time", "25", url], capture_output=True)
    return r.stdout


def parse_feed(name, data, force_cat=None):
    items = []
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        return items
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        desc = re.sub(r"<[^>]+>", "", (it.findtext("description") or "")).strip()
        desc = re.sub(r"\s+", " ", desc)[:110]
        if title and link:
            items.append({"title": title, "link": link, "source": name,
                          "pub": pub, "origin": "rss", "summary": desc, "force_cat": force_cat})
    return items


def normalize(title):
    t = unicodedata.normalize("NFKC", title).lower()
    t = re.sub(r"[\s　・:：/／|｜\-―ー「」『』【】()（）!?！？,、。.]+", "", t)
    return t


def classify(title):
    best, best_score = "その他", 0
    for cat, kws in CATEGORIES:
        s = sum(2 for kw in kws if kw.lower() in title.lower())
        if s > best_score:
            best, best_score = cat, s
    score = best_score + sum(1 for kw in GENERIC_BOOST if kw in title)
    return best, score


def token_set(title):
    t = unicodedata.normalize("NFKC", title)
    return set(re.findall(r"[A-Za-z0-9]{2,}|[ぁ-んァ-ヶ一-龥]{2,}", t))


def dedupe_and_score(items):
    seen = {}
    order = []
    for it in items:
        n = normalize(it["title"])
        if n in seen:
            seen[n]["dup_count"] = seen[n].get("dup_count", 1) + 1
            continue
        seen[n] = it
        order.append(it)
    for i, a in enumerate(order):
        ta = token_set(a["title"])
        if not ta:
            continue
        for b in order[i + 1:]:
            tb = token_set(b["title"])
            if not tb:
                continue
            j = len(ta & tb) / max(1, min(len(ta), len(tb)))
            if j >= 0.5:
                a["score"] = a.get("score", 0) + 3
                b["score"] = b.get("score", 0) + 3
    return order


def fx2_top(query):
    try:
        r = subprocess.run(["python3", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fx2.py"),
                            "search", query],
                           capture_output=True, text=True, timeout=60)
        out = r.stdout
    except Exception:
        return []
    posts = []
    blocks = re.split(r"\n- https://x\.com/", out)
    for b in blocks[1:]:
        lines = b.splitlines()
        url = "https://x.com/" + lines[0].strip()
        text = "\n".join(lines[1:])
        if any(p in text for p in SPAM_PATTERNS):
            continue
        m_like = re.search(r"いいね(\d+)", text)
        m_view = re.search(r"表示(\d+)", text)
        likes = int(m_like.group(1)) if m_like else 0
        views = int(m_view.group(1)) if m_view else 0
        if views < 1000 and likes < 5:
            continue
        body = lines[2] if len(lines) > 2 else ""
        beat_words = ["Claude", "Windows", "iPhone", "Android", "パスワード", "漏えい", "不具合", "障害", "バッテリー", "スマホ", "PC", "アップデート", "Wi-Fi"]
        if not any(w.lower() in text.lower() for w in beat_words):
            continue
        posts.append({"title": body[:90], "link": url, "source": "X:" + query,
                      "pub": "", "origin": "x", "likes": likes, "views": views,
                      "score": likes * 2 + views // 200})
    return posts


def polish(items):
    pool = [i for i in items if not i.get("related_to")]
    by_cat = {}
    for i in pool:
        by_cat.setdefault(i["category"], []).append(i)
    targets = []
    cats = list(by_cat.keys())
    while len(targets) < 14 and any(by_cat[c] for c in cats):
        for c in cats:
            if by_cat[c] and len(targets) < 14:
                targets.append(by_cat[c].pop(0))
    if not targets:
        return
    payload = [{"title": t["title"], "raw": t.get("summary", "")} for t in targets]
    prompt = ("次のニュース一覧について、各項目の内容を日本語1文（60字以内）で要約してください。"
              "元の書き出しが途中で切れている場合は、見出しの範囲で補ってください。推測で数字を作らないこと。"
              "出力はJSON配列のみ（titleはそのまま、summaryに要約）。\n" + json.dumps(payload, ensure_ascii=False))
    try:
        r = subprocess.run([(shutil.which("claude") or "/home/hatch/.local/bin/claude"), "-p", prompt],
                           capture_output=True, text=True, timeout=240)
        m = re.search(r"\[.*\]", r.stdout, re.S)
        arr = json.loads(m.group(0)) if m else []
        by_title = {a.get("title"): a.get("summary") for a in arr}
        for t in targets:
            if by_title.get(t["title"]):
                t["summary"] = by_title[t["title"]]
                t["polished"] = True
    except Exception:
        pass


def main():
    today = datetime.now(JST).strftime("%Y-%m-%d")
    items = []
    for name, url, force in FEEDS:
        items.extend(parse_feed(name, fetch(url), force))
    now = datetime.now(JST)
    fresh = []
    for it in items:
        try:
            dt = parsedate_to_datetime(it["pub"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if (now - dt.astimezone(JST)).total_seconds() > WINDOW_H.get(it["source"], 72) * 3600:
                continue
        except Exception:
            pass
        fresh.append(it)
    items = [it for it in fresh
             if not (it["source"].startswith("GN:")
                     and any(w in it["title"] for w in OTHER_EXAM_WORDS)
                     and not any(a in it["title"] for a in IT_ANCHORS))]
    for it in items:
        cat, sc = classify(it["title"])
        if it.get("force_cat"):
            cat = it["force_cat"]
            sc = max(sc, 4)
        it["category"], it["score"] = cat, min(sc, 8) + it.get("score", 0)
        if any(w in it["title"] for w in PR_WORDS):
            it["score"] -= 4
        for kw, angle in NETA_RULES:
            if kw in it["title"]:
                it["neta_candidate"] = angle
                break
    from collections import Counter as _C
    _used = _C()
    for it in sorted(items, key=lambda x: -x.get("score", 0)):
        ang = it.get("neta_candidate")
        if ang and it.get("score", 0) >= 5 and _used[ang] < 2:
            it["neta"] = ang
            it["score"] += 2
            _used[ang] += 1
    items = dedupe_and_score(items)
    items.sort(key=lambda x: -x.get("score", 0))
    for i, a in enumerate(items):
        ta = token_set(a["title"])
        if not ta:
            continue
        for b in items[i + 1:]:
            if b.get("related_to"):
                continue
            tb = token_set(b["title"])
            if tb and len(ta & tb) / max(1, min(len(ta), len(tb))) >= 0.45:
                b["related_to"] = a["title"]

    xposts = []
    for q in X_QUERIES:
        xposts.extend(fx2_top(q))
    xposts.sort(key=lambda x: -x.get("score", 0))
    xposts = xposts[:6]

    if "--polish" in sys.argv:
        polish(items)
    lines = ["# デジタル地下室ブリーフィング（v1.7）― " + today, ""]
    for cat, _ in CATEGORIES + [("その他", [])]:
        group = [i for i in items if i["category"] == cat and i.get("score", 0) > 0 and not i.get("related_to")][:5]
        if not group:
            continue
        lines.append("## " + cat)
        for i in group:
            dup = "（%d媒体）" % i["dup_count"] if i.get("dup_count", 1) > 1 else ""
            lines.append("- **" + i["title"] + "**" + dup + "（" + i["source"] + "）")
            if i.get("summary"):
                lines.append("  " + i["summary"])
            lines.append("  " + i["link"])
            if i.get("neta"):
                lines.append("  📌 ネタ候補: " + i["neta"])
            for r in [x for x in items if x.get("related_to") == i["title"]]:
                lines.append("  - 関連: " + r["title"] + "（" + r["source"] + "） " + r["link"])
        lines.append("")
    if xposts:
        lines.append("## Xでの観測（注目順・足切り済み）")
        for p in xposts:
            lines.append("- " + p["title"] + "（いいね%d／表示%d）" % (p["likes"], p["views"]))
            lines.append("  " + p["link"])
        lines.append("")
    lines.append("---")
    lines.append("収集: RSS %d件（重複排除後）／ X観測 %d件" % (len(items), len(xposts)))

    out_dir = os.environ.get("BRIEFING_OUT", os.path.dirname(os.path.abspath(__file__)))
    out_md = os.path.join(out_dir, "briefing-" + today + ".md")
    with open(out_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    import html as _h
    def esc(x):
        return _h.escape(x or "")
    CAT_COLORS = {"セキュリティ": "#e74c3c", "AI・Muse": "#a569bd", "スマホ・PC": "#3498db", "IT資格": "#2ecc71", "その他": "#95a5a6"}
    CSS = ("body{margin:0;background:#0d0f14;color:#e8e6df;font-family:'Hiragino Sans','Noto Sans JP',sans-serif}"
           ".wrap{max-width:860px;margin:0 auto;padding:28px 18px 48px}"
           "header{display:flex;align-items:center;gap:14px;border-bottom:1px solid #2a2e3a;padding-bottom:16px}"
           "header img{width:56px;height:56px;border-radius:50%;border:2px solid #e8b64c}"
           "header h1{font-size:24px;margin:0;color:#fff}"
           "header .sub{font-family:monospace;color:#e8b64c;font-size:12.5px;letter-spacing:.08em}"
           ".top{margin-top:20px;background:linear-gradient(135deg,#1a1408,#241c0d);border:1px solid #e8b64c;border-radius:12px;padding:20px}"
           ".top .k{color:#e8b64c;font-family:monospace;font-size:12px;letter-spacing:.25em}"
           ".top h2{font-size:22px;line-height:1.5;margin:8px 0;color:#fff}"
           ".top h2 a{color:#fff;text-decoration:none}"
           ".top p{font-size:14.5px;line-height:1.8;color:#cfcaba;margin:0}"
           ".cat{margin-top:28px}"
           ".cat h3{font-size:17px;margin:0 0 12px;color:#fff}"
           ".cat h3 .bar{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:8px}"
           ".grid{display:grid;grid-template-columns:1fr;gap:10px}"
           "@media(min-width:700px){.grid{grid-template-columns:1fr 1fr}}"
           ".card{background:#151822;border:1px solid #2a2e3a;border-radius:10px;padding:14px 16px}"
           ".card h4{margin:0 0 6px;font-size:15.5px;line-height:1.55}"
           ".card h4 a{color:#f2efe6;text-decoration:none}"
           ".card p{margin:0;font-size:13px;line-height:1.7;color:#a9a695}"
           ".src{font-family:monospace;font-size:11px;color:#7d7a6d}"
           ".neta{display:inline-block;margin-top:8px;background:#2b2107;border:1px solid #e8b64c;color:#e8b64c;font-size:12px;padding:2px 9px;border-radius:20px}"
           ".rel{font-size:12px;color:#8b887a;margin-top:6px}.rel a{color:#a9a695}"
           "footer{text-align:center;color:#5d5b52;font-size:12px;margin-top:36px;font-family:monospace}")
    groups = []
    for cat, _ in CATEGORIES + [("その他", [])]:
        g = [i for i in items if i["category"] == cat and i.get("score", 0) > 0 and not i.get("related_to")][:5]
        if g:
            groups.append((cat, g))
    top_item = None
    for i in items:
        if i.get("score", 0) > 0 and not i.get("related_to"):
            top_item = i
            break
    def card(i, in_top=False):
        s = "<h4><a href=\"" + esc(i["link"]) + "\">" + esc(i["title"]) + "</a></h4>"
        if i.get("summary"):
            s += "<p>" + esc(i["summary"]) + "</p>"
        s += "<span class=\"src\">" + esc(i["source"]) + "</span>"
        if i.get("neta"):
            s += "<br><span class=\"neta\">📌 ネタ候補: " + esc(i["neta"]) + "</span>"
        for r in [x for x in items if x.get("related_to") == i["title"]]:
            s += "<div class=\"rel\">関連: <a href=\"" + esc(r["link"]) + "\">" + esc(r["title"]) + "</a>（" + esc(r["source"]) + "）</div>"
        return s
    parts = ["<!DOCTYPE html><html lang=\"ja\"><head><meta charset=\"utf-8\">",
             "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
             "<title>デジタル地下室ブリーフィング " + today + "</title>",
             "<style>" + CSS + "</style></head><body><div class=\"wrap\">",
             "<header><img src=\"/face-smile.webp\" alt=\"\"><div><h1>デジタル地下室ブリーフィング</h1>",
             "<div class=\"sub\">DAILY BRIEF // " + today + " // 07:00 JST AUTO</div></div></header>"]
    if top_item is not None:
        t = "<section class=\"top\"><div class=\"k\">TOP STORY</div><h2><a href=\"" + esc(top_item["link"]) + "\">" + esc(top_item["title"]) + "</a></h2>"
        if top_item.get("summary"):
            t += "<p>" + esc(top_item["summary"]) + "</p>"
        t += "<span class=\"src\">" + esc(top_item["source"]) + "</span>"
        if top_item.get("neta"):
            t += "<br><span class=\"neta\">📌 ネタ候補: " + esc(top_item["neta"]) + "</span>"
        t += "</section>"
        parts.append(t)
    for cat, g in groups:
        parts.append("<section class=\"cat\"><h3><span class=\"bar\" style=\"background:" + CAT_COLORS.get(cat, "#e8b64c") + "\"></span>" + esc(cat) + "</h3><div class=\"grid\">")
        for i in g:
            if top_item is not None and i is top_item:
                continue
            parts.append("<article class=\"card\">" + card(i) + "</article>")
        parts.append("</div></section>")
    if xposts:
        parts.append("<section class=\"cat\"><h3><span class=\"bar\" style=\"background:#e8b64c\"></span>Xでの観測</h3><div class=\"grid\">")
        for p in xposts:
            parts.append("<article class=\"card\"><h4><a href=\"" + esc(p["link"]) + "\">" + esc(p["title"]) + "</a></h4><span class=\"src\">いいね" + str(p["likes"]) + " / 表示" + str(p["views"]) + "</span></article>")
        parts.append("</div></section>")
    parts.append("<footer>DIGITAL BASEMENT — DAILY BRIEF // RSS " + str(len(items)) + "件 ・ X観測 " + str(len(xposts)) + "件</footer></div></body></html>")
    out_html = os.path.join(out_dir, "briefing-" + today + ".html")
    with open(out_html, "w", encoding="utf-8") as f:
        f.write("".join(parts))
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write("".join(parts))
    with open(os.path.join(out_dir, "items.json"), "w", encoding="utf-8") as f:
        json.dump({"rss": items, "x": xposts}, f, ensure_ascii=False, indent=1)
    print("wrote %s (rss=%d, x=%d)" % (out_md, len(items), len(xposts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
