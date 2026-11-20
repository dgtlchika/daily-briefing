#!/usr/bin/env python3
"""日本語テクノロジー・ブリーフィング生成（試作v1.2）"""
import csv
import json
import os
import shutil
import re
import subprocess
import sys
import tempfile
import unicodedata
import urllib.parse
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
# v1.8: ネタ旗の同義語（キーワード → 追加でマッチさせる語）
NETA_SYNONYMS = {
    "パスワード": ["パスキー", "二段階認証", "多要素認証"],
    "漏えい": ["漏洩", "流出", "窃取"],
    "障害": ["通信障害", "システム障害", "つながらない", "繋がらない", "ダウン"],
    "不具合": ["バグ", "不調", "エラー"],
    "アップデート": ["更新プログラム", "アプデ", "パッチ"],
    "バッテリー": ["電池"],
    "詐欺": ["フィッシング", "なりすまし", "偽サイト", "偽メール"],
}
NETA_MIN_SCORE = 3  # v1.5の5から緩和
NETA_MIN_SCORE_BASE = 2  # v1.9.1: 基本キーワード自体が一致した候補の下限（同義語のみの一致は NETA_MIN_SCORE のまま）
# v1.8: 「新しい材料」を示す語。これがあれば前日と同じ角度でも旗を立て直す
NEW_MATERIAL_WORDS = ["続報", "更新", "判明", "拡大", "原因", "復旧", "再発", "追加", "新たに",
                      "公表", "謝罪", "対策", "提供開始", "修正", "被害額", "件数"]
STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
LOOKBACK_DAYS = 8   # 直近8日分と照合（GN資格系の鮮度窓7日をカバー）
KEEP_DAYS = 14      # これを超えた state ファイルは削除
FOLLOWUP_RATIO = 0.5

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


def overlap(ta, tb):
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, min(len(ta), len(tb)))


def url_key(link):
    """http/https・www.・クエリ・フラグメント・末尾スラッシュ・index.html の差を無視"""
    u = (link or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.split(r"[?#]", u, 1)[0]
    if u.startswith("www."):
        u = u[4:]
    u = re.sub(r"/index\.html?$", "", u)
    return u.rstrip("/")


def neta_angle(title):
    for kw, angle in NETA_RULES:
        if kw in title:
            return angle, True
        if any(k in title for k in NETA_SYNONYMS.get(kw, [])):
            return angle, False
    return None, False


def load_history(today, out_dir):
    """直近 LOOKBACK_DAYS 日分（今日を除く）の state を読む。state が空なら公開側 items.json を前日分として取り込む"""
    os.makedirs(STATE_DIR, exist_ok=True)
    files = sorted(f for f in os.listdir(STATE_DIR) if re.fullmatch(r"items-\d{4}-\d{2}-\d{2}\.json", f))
    if not files:
        pub = os.path.join(out_dir, "items.json")
        if os.path.exists(pub):
            try:
                with open(pub, encoding="utf-8") as f:
                    rss = json.load(f).get("rss", [])
                d = datetime.fromtimestamp(os.path.getmtime(pub), JST).date()
                t = datetime.strptime(today, "%Y-%m-%d").date()
                if d >= t:
                    d = t - timedelta(days=1)
                ds = d.strftime("%Y-%m-%d")
                seed = {"date": ds, "seeded_from": pub,
                        "candidates": [{"title": i["title"], "link": i["link"], "source": i.get("source", "")} for i in rss],
                        "flags": [{"title": i["title"], "link": i["link"], "angle": i["neta"], "date": ds}
                                  for i in rss if i.get("neta")]}
                with open(os.path.join(STATE_DIR, "items-" + ds + ".json"), "w", encoding="utf-8") as f:
                    json.dump(seed, f, ensure_ascii=False, indent=1)
                files = ["items-" + ds + ".json"]
                print("dedupe: seeded state from %s as %s (%d items)" % (pub, ds, len(rss)))
            except Exception as e:
                print("dedupe: seed failed: %s" % e)
    t = datetime.strptime(today, "%Y-%m-%d").date()
    hist = []
    for fn in files:
        d = datetime.strptime(fn[6:16], "%Y-%m-%d").date()
        if 1 <= (t - d).days <= LOOKBACK_DAYS:
            try:
                with open(os.path.join(STATE_DIR, fn), encoding="utf-8") as f:
                    hist.append(json.load(f))
            except Exception:
                pass
    return hist


def save_state(today, candidates, flags):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(os.path.join(STATE_DIR, "items-" + today + ".json"), "w", encoding="utf-8") as f:
        json.dump({"date": today, "candidates": candidates, "flags": flags}, f, ensure_ascii=False, indent=1)
    t = datetime.strptime(today, "%Y-%m-%d").date()
    for fn in os.listdir(STATE_DIR):
        if re.fullmatch(r"items-\d{4}-\d{2}-\d{2}\.json", fn):
            if (t - datetime.strptime(fn[6:16], "%Y-%m-%d").date()).days > KEEP_DAYS:
                os.remove(os.path.join(STATE_DIR, fn))


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


# ===== v1.9: 今日の1本（todays_pick_design.md）=====
_HERE = os.path.dirname(os.path.abspath(__file__))
NOTE_MASTER_PATH = os.environ.get("BRIEFING_NOTE_MASTER", os.path.join(_HERE, "data", "note_master_list.csv"))
PICK_HISTORY_PATH = os.environ.get("BRIEFING_PICK_HISTORY", os.path.join(_HERE, "data", "todays_pick_history.json"))
PICK_SHADOW = os.environ.get("BRIEFING_PICK_SHADOW", "1") != "0"   # 既定はシャドー（記録のみ・非表示）
PICK_MIN_SCORE = 45
PICK_REPEAT_DAYS = 30
PICK_KEEP_DAYS = 90
PICK_MAX_AGE_DAYS = 14      # スナップショットの鮮度
PICK_MIN_ROWS = 50          # 有効行がこれ未満なら行数異常として選定しない
PICK_URL_PREFIX = "https://note.com/sumaho_lab/n/"
PICK_REQUIRED_COLS = ["記事ID", "タイトル", "URL", "価格", "カテゴリ推定"]
PICK_UTM = [("utm_source", "briefing"), ("utm_medium", "daily_brief"), ("utm_campaign", "todays_pick")]
# 4.2 カテゴリ対応表（note側 カテゴリ推定 → ブリーフィング側カテゴリ）
PICK_CAT_MAP = {
    "セキュリティ": "セキュリティ", "アカウント・ログイン": "セキュリティ",
    "AI・自動化": "AI・Muse",
    "iPhone・スマホ": "スマホ・PC", "Mac・PC": "スマホ・PC", "Windows": "スマホ・PC",
    "Wi-Fi・ネットワーク": "スマホ・PC", "充電・電池": "スマホ・PC", "容量・ストレージ": "スマホ・PC",
    "プリンター・周辺機器": "スマホ・PC", "Office・文書": "スマホ・PC",
    "ITパスポート": "IT資格", "基本情報技術者": "IT資格", "AWS": "IT資格", "AWS SAA": "IT資格",
    "AWS SAP": "IT資格", "G検定": "IT資格",
    # 設計メモの表にはないがCSVに実在する同系統の資格カテゴリ（v1.9で明示追加）
    "AWS AIF": "IT資格", "AWS CLF": "IT資格", "AWS DVA": "IT資格", "応用情報技術者": "IT資格",
    "生活・家電の仕組み": "その他",
    # VPS・サーバー構築：対応カテゴリなし。AI・Muse／スマホ・PCの話題とタイトル語が重なる場合だけ候補
}
PICK_VPS_CAT = "VPS・サーバー構築"
# カテゴリ空欄・未知の補完（具体カテゴリを先に判定する）
PICK_TITLE_CATS = [
    ("AWS", ["AWS"]), ("基本情報技術者", ["基本情報", "FE科目", "科目B"]), ("応用情報技術者", ["応用情報"]),
    ("ITパスポート", ["ITパスポート"]), ("G検定", ["G検定", "DS検定"]),
    ("AI・自動化", ["ChatGPT", "Claude", "Gemini", "NotebookLM", "生成AI", "AI", "Apps Script"]),
    ("アカウント・ログイン", ["パスキー", "パスワード", "二段階認証", "ログイン", "アカウント", "メールアドレス"]),
    ("セキュリティ", ["https", "鍵マーク", "迷惑メール", "詐欺", "フィッシング", "シークレットモード", "短縮URL",
                     "ウイルス", "セキュリティ", "乗っ取り", "高度な保護"]),
    ("充電・電池", ["充電", "バッテリー", "電池"]),
    ("容量・ストレージ", ["SSD", "HDD", "容量", "ストレージ", "iCloud"]),
    ("Windows", ["Windows"]), ("Office・文書", ["Excel", "Word", "PDF", "Outlook", "スプレッドシート"]),
    ("Mac・PC", ["Mac", "パソコン", "キーボード", "PC"]),
    ("iPhone・スマホ", ["iPhone", "Android", "スマホ", "ガラケー", "携帯", "おやすみモード", "緊急速報"]),
    ("Wi-Fi・ネットワーク", ["Wi-Fi", "光回線", "回線", "電波", "圏外", "プラチナバンド", "通信", "ネット"]),
    ("生活・家電の仕組み", ["冷蔵庫", "エアコン", "洗濯機", "加湿器", "IH", "LED", "テレビ", "電波時計", "GPS", "QRコード", "バーコード"]),
    ("VPS・サーバー構築", ["VPS", "サーバー"]),
]
# 一般語（これだけの一致は数えない）
PICK_STOPWORDS = {
    "方法", "仕組", "理由", "原因", "確認", "手順", "設定", "注意", "解説", "公開", "発表", "開始", "終了", "記事",
    "無料", "有料", "話題", "場合", "情報", "技術", "対応", "機能", "利用", "問題", "日本", "今日", "本当", "意外",
    "正体", "意味", "必要", "対策", "自分", "完全", "最新", "年版", "演習", "世界", "可能", "提供", "予定", "発売",
    "判明", "影響", "一部", "全国", "国内", "海外", "企業", "会社", "発生", "実施", "開発", "導入", "検討", "報告",
    "調査", "結果", "状況", "時代", "理解", "簡単", "違", "大丈夫", "月", "年", "日",
    "チェックリスト", "ポイント", "ニュース", "サービス", "ユーザー", "チートシート", "シリーズ",
    "pr", "vs", "ok", "the", "and", "of", "to", "in", "for", "on", "new", "ver", "no",
}
# 語彙（長い漢字列の中に埋もれた語も拾う）
PICK_VOCAB = sorted({kw for _, kws in CATEGORIES for kw in kws} | {kw for kw, _ in NETA_RULES}
                    | {w for ws in NETA_SYNONYMS.values() for w in ws}
                    | {w for _, ws in PICK_TITLE_CATS for w in ws} | {"基本情報技術者", "応用情報技術者"})
PICK_CAT_BLURB = {
    "セキュリティ": "今日の話題に関連する仕組みと、利用者側の確認ポイントを掘り下げる1本です。",
    "AI・Muse": "今日のAI関連ニュースの背景にある仕組みや使い方を掘り下げる1本です。",
    "スマホ・PC": "今日の話題に近い症状・仕組みを、手順寄りに掘り下げる1本です。",
    "IT資格": "今日の資格ニュースに関連する範囲や考え方を掘り下げる1本です。",
}


class PickSkip(Exception):
    """枠なしにする理由（異常ではなく、選定しない判断）"""


def _d(s):
    return datetime.strptime(s[:10], "%Y-%m-%d").date()


def pick_terms(title):
    """noteタイトル／ニュースタイトルから照合用の語を取り出す（表層形のまま）"""
    t = unicodedata.normalize("NFKC", title or "")
    terms = set(re.findall(r"[A-Za-z][A-Za-z0-9]+", t))
    terms |= set(w.lstrip("ー") for w in re.findall(r"[ァ-ヶー]{2,}", t))
    terms |= set(re.findall(r"[一-龥々]{2,}", t))
    terms |= {w for w in PICK_VOCAB if w in t}
    return {w for w in terms if len(w) >= 2 and w.lower() not in PICK_STOPWORDS and w not in PICK_STOPWORDS}


def _is_ascii(w):
    return bool(re.fullmatch(r"[A-Za-z0-9\-]+", w))


def match_terms(note_terms, text, ascii_set):
    """note側の語のうち、文脈（ニュース等のタイトル連結）に出る語。英数字は語単位・大文字小文字無視、和語は部分一致"""
    hit = {w for w in note_terms if (w.lower() in ascii_set if _is_ascii(w) else w in text)}
    return {w for w in hit if not any(w != o and w in o for o in hit)}   # 包含される短い語は数えない


def term_weight(ws):
    return min(6, sum(2 if _is_ascii(w) else 1 for w in ws))   # 英数字（製品名・試験名など）は重く、記事ごとに上限6


def note_category_for(row):
    """(noteカテゴリ, 対応するブリーフィング側カテゴリ or None, 補完したか)"""
    cat = (row.get("カテゴリ推定") or "").strip()
    inferred = False
    if cat not in PICK_CAT_MAP and cat != PICK_VPS_CAT and not cat.startswith("AWS"):
        title = unicodedata.normalize("NFKC", row.get("タイトル") or "")
        cat = next((c for c, ws in PICK_TITLE_CATS if any(w in title for w in ws)), "")
        inferred = True
    brief = PICK_CAT_MAP.get(cat) or ("IT資格" if cat.startswith("AWS") else None)
    return cat, brief, inferred


def load_note_candidates(path, today):
    """3節のデータ品質ゲート。満たさなければ PickSkip。不正行は除外して件数を返す"""
    if not os.path.exists(path):
        raise PickSkip("note master not found: %s" % path)
    meta_path = re.sub(r"\.csv$", "", path) + ".meta.json"
    meta = {}
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    if meta.get("exported_at"):
        exported = datetime.fromisoformat(meta["exported_at"]).astimezone(JST).date()
    else:
        exported = datetime.fromtimestamp(os.path.getmtime(path), JST).date()
    age = (_d(today) - exported).days
    if age > PICK_MAX_AGE_DAYS:
        raise PickSkip("note master snapshot too old (%d days, exported %s)" % (age, exported))
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = [c for c in PICK_REQUIRED_COLS if c not in (reader.fieldnames or [])]
        if missing:
            raise PickSkip("note master missing columns: %s" % ",".join(missing))
        rows = list(reader)
    if meta.get("rows_excl_header") is not None and int(meta["rows_excl_header"]) != len(rows):
        raise PickSkip("note master row count %d != meta %s" % (len(rows), meta["rows_excl_header"]))
    ids = [(r.get("記事ID") or "").strip() for r in rows]
    dup = sorted({i for i in ids if i and ids.count(i) > 1})
    if dup:
        raise PickSkip("note master duplicate article IDs: %s" % ",".join(dup[:5]))
    cands, bad = [], {"empty": 0, "price": 0, "url": 0}
    for r in rows:
        aid, title, url = [(r.get(k) or "").strip() for k in ("記事ID", "タイトル", "URL")]
        if not (aid and title and url):
            bad["empty"] += 1
            continue
        if not url.startswith(PICK_URL_PREFIX):
            bad["url"] += 1
            continue
        try:
            price = int((r.get("価格") or "").strip())
            if price < 0:
                raise ValueError
        except ValueError:
            bad["price"] += 1
            continue
        cat, brief, inferred = note_category_for(r)
        m = re.search(r"公開日\s*(\d{4}-\d{2}-\d{2})", r.get("備考") or "")
        cands.append({"article_id": aid, "title": title, "url": url, "price": price, "is_paid": price > 0,
                      "note_category": cat, "brief_category": brief, "category_inferred": inferred,
                      "published": m.group(1) if m else None, "terms": pick_terms(title)})
    print("pick: note master rows=%d valid=%d invalid=%d (empty=%d price=%d url=%d) snapshot=%s"
          % (len(rows), len(cands), sum(bad.values()), bad["empty"], bad["price"], bad["url"], exported))
    if len(cands) < PICK_MIN_ROWS:
        raise PickSkip("too few valid note rows (%d)" % len(cands))
    return cands


def build_pick_context(items, xposts):
    """実際に表示されるものだけを材料にする（関連として束ねた側・スコア0以下は除外）"""
    shown = []
    for cat, _ in CATEGORIES + [("その他", [])]:
        shown += [i for i in items if i["category"] == cat and i.get("score", 0) > 0 and not i.get("related_to")][:5]
    top = next((i for i in items if i.get("score", 0) > 0 and not i.get("related_to")), None)
    from collections import Counter as _C
    dist = _C(i["category"] for i in shown)
    counts = sorted(set(dist.values()), reverse=True)
    rank = {c: counts.index(n) + 1 for c, n in dist.items()}
    def ctx_text(titles):
        tl = [unicodedata.normalize("NFKC", t) for t in titles]
        return "\n".join(tl), {w.lower() for t in tl for w in re.findall(r"[A-Za-z][A-Za-z0-9]+", t)}
    news_titles = [i["title"] for i in shown]
    x_titles = [p["title"] for p in xposts]
    return {
        "rank": rank, "dist": dist, "n_shown": len(shown),
        "top_category": top["category"] if top else None,
        "all": ctx_text(news_titles + x_titles), "x": ctx_text(x_titles),
        "vps": ctx_text([i["title"] for i in shown if i["category"] in ("AI・Muse", "スマホ・PC")]),
        "neta": [(i["neta"], i["category"]) for i in shown if i.get("neta")],
    }


def ratio_adjustment(history, today):
    """直近7回の掲載で無料4：有料3を目安に、無料候補へ加える点（有料は符号反転）。±8で頭打ち"""
    recent = sorted((h for h in history if h.get("date") < today and h.get("pick")), key=lambda h: h["date"])[-7:]
    n = len(recent)
    free = sum(1 for h in recent if not h["pick"].get("is_paid"))
    adj = int(round(4 * (4.0 * n / 7 - free)))
    return max(-8, min(8, adj)), free, n - free


def score_note_candidates(ctx, cands, history, today):
    t = _d(today)
    last_shown = {}
    for h in history:
        if h.get("pick"):
            aid = h["pick"]["article_id"]
            last_shown[aid] = max(last_shown.get(aid, ""), h["date"])
    prev = {h["date"]: h["pick"].get("note_category") for h in history if h.get("pick")}
    d1, d2 = [(t - timedelta(days=k)).strftime("%Y-%m-%d") for k in (1, 2)]
    streak_cat = prev.get(d1) if prev.get(d1) and prev.get(d1) == prev.get(d2) else None
    top_share = (max(ctx["dist"].values()) / ctx["n_shown"]) if ctx["n_shown"] else 0
    top_cat = ctx["dist"].most_common(1)[0][0] if ctx["dist"] else None
    adj_free, n_free, n_paid = ratio_adjustment(history, today)
    out, reasons = [], {"repeat": 0, "below_min": 0, "no_evidence": 0, "vps_no_overlap": 0}
    for c in cands:
        ls = last_shown.get(c["article_id"])
        if ls and (t - _d(ls)).days < PICK_REPEAT_DAYS:
            reasons["repeat"] += 1
            continue
        b = c["brief_category"]
        r = ctx["rank"].get(b) if b else None
        cat_pts = {1: 35, 2: 22}.get(r, 10) if r else 0
        hit = match_terms(c["terms"], *ctx["all"])
        raw = term_weight(hit)
        term_pts = min(30, int(round(30.0 * raw / (3 + 0.25 * len(c["terms"])))))
        xhit = match_terms(c["terms"], *ctx["x"])
        x_pts = min(10, 5 * term_weight(xhit))
        neta_pts = 0
        for ang, ncat in ctx["neta"]:
            kw = next((k for k, a in NETA_RULES if a == ang), None)
            words = ([kw] + NETA_SYNONYMS.get(kw, [])) if kw else []
            if any(w in c["title"] for w in words):
                neta_pts = 15
            elif b and b == ncat:
                neta_pts = max(neta_pts, 5)
        expl = 10 if not ls else min(8, (t - _d(ls)).days // 10)
        rec = 0
        if c["published"]:
            age = (t - _d(c["published"])).days
            rec = 5 if age <= 7 else 3 if age <= 30 else 1 if age <= 90 else 0
        top_bonus = 5 if b and b == ctx["top_category"] else 0
        streak = -10 if (streak_cat and c["note_category"] == streak_cat
                         and not (top_share >= 0.5 and b == top_cat)) else 0
        relevance = min(100, cat_pts + term_pts + neta_pts + x_pts + expl + rec + top_bonus) + streak
        ratio = -adj_free if c["is_paid"] else adj_free
        score = max(0, min(100, relevance + ratio))
        if c["note_category"] == PICK_VPS_CAT and not match_terms(c["terms"], *ctx["vps"]):
            reasons["vps_no_overlap"] += 1
            continue
        if not (cat_pts > 0 or term_pts > 0):
            reasons["no_evidence"] += 1
            continue
        if min(score, relevance) < PICK_MIN_SCORE:   # 比率調整で足切りを越えさせない
            reasons["below_min"] += 1
            continue
        out.append(dict(c, score=score, relevance=relevance, terms_raw=raw, last_shown=ls or "",
                        matched_terms=sorted(hit, key=lambda w: (-len(w), w)),
                        matched_categories=[b] if cat_pts else [],
                        score_breakdown={"category": cat_pts, "terms": term_pts, "neta": neta_pts, "x": x_pts,
                                         "exploration": expl, "recency": rec, "top_story": top_bonus,
                                         "category_streak": streak, "ratio_adjustment": ratio}))
    return out, reasons, (adj_free, n_free, n_paid)


def pick_utm_url(url, article_id):
    sp = urllib.parse.urlsplit(url)
    if sp.scheme != "https" or not sp.netloc or not article_id:
        raise ValueError("bad url for utm: %r" % url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(sp.query, keep_blank_values=True) if not k.startswith("utm_")]
    q += PICK_UTM + [("utm_content", article_id)]
    return urllib.parse.urlunsplit((sp.scheme, sp.netloc, sp.path, urllib.parse.urlencode(q), sp.fragment))


def pick_one_liner(c):
    """5節の定型文。本文の体裁は約束しない"""
    natural = [w for w in c["matched_terms"] if w in PICK_VOCAB or (len(w) >= 3 and not w.isdigit())]
    if natural:
        return "今日の話題「%s」を、もう一歩掘り下げる1本です。" % natural[0]
    b = c["matched_categories"][0] if c["matched_categories"] else c["brief_category"]
    if b in PICK_CAT_BLURB:
        return PICK_CAT_BLURB[b]
    if c["matched_terms"]:
        return "今日の話題「%s」を、もう一歩掘り下げる1本です。" % c["matched_terms"][0]
    return "今日の話題に関連する仕組みを掘り下げる1本です。"


def choose_todays_pick(scored, today):
    """4.7節の決定順。比率調整後の首位より関連性が15点以上高い候補があれば、そちらを優先"""
    if not scored:
        return None
    key = lambda c: (-c["score"], -c["terms_raw"], c["last_shown"], c["article_id"])
    ranked = sorted(scored, key=key)
    win = ranked[0]
    best_rel = min(scored, key=lambda c: (-c["relevance"], -c["terms_raw"], c["last_shown"], c["article_id"]))
    if best_rel["relevance"] >= win["relevance"] + 15:
        win = best_rel
    pick = {"date": today, "article_id": win["article_id"], "title": win["title"], "url": win["url"],
            "linked_url": pick_utm_url(win["url"], win["article_id"]), "price": win["price"],
            "is_paid": win["is_paid"], "note_category": win["note_category"],
            "matched_categories": win["matched_categories"], "matched_terms": win["matched_terms"],
            "score": win["score"], "score_breakdown": win["score_breakdown"], "one_liner": pick_one_liner(win)}
    top5 = [{"article_id": c["article_id"], "title": c["title"][:40], "score": c["score"],
             "relevance": c["relevance"], "breakdown": c["score_breakdown"]} for c in ranked[:5]]
    return pick, top5


def load_pick_history(path):
    """壊れていれば例外（呼び出し側で枠なし・上書きしない）"""
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError("unexpected structure")
    for h in data["entries"]:
        _d(h["date"])
    return data["entries"]


def commit_pick_history(path, history, entry, today):
    """出力書き込み成功後に呼ぶ。同日分があれば何もしない。90日分を保持し、一時ファイル＋置換で原子的に書く"""
    if any(h.get("date") == today for h in history):
        return False
    t = _d(today)
    keep = [h for h in history if (t - _d(h["date"])).days <= PICK_KEEP_DAYS] + [entry]
    keep.sort(key=lambda h: h["date"])
    fd, tmp = tempfile.mkstemp(prefix=".todays_pick_history.", suffix=".tmp", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "entries": keep}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return True


def render_pick(pick):
    """(Markdown行, HTML断片)。表示用URLはUTM付き linked_url に統一"""
    import html as _h
    price_label = "有料 ¥{:,}".format(pick["price"]) if pick["is_paid"] else "無料"
    cat = pick.get("note_category") or ""
    md_title = pick["title"].replace("[", "\\[").replace("]", "\\]")
    md = ["## 今日の1本",
          "- **[" + md_title + "](" + pick["linked_url"] + ")**",
          "  " + pick["one_liner"] + "（" + ((cat + "／") if cat else "") + price_label + "）",
          ""]
    u = _h.escape(pick["linked_url"])
    h = ("<section class=\"pick\"><div class=\"k\">TODAY'S PICK // 今日の1本</div>"
         "<h2><a href=\"" + u + "\">" + _h.escape(pick["title"]) + "</a></h2>"
         "<p>" + _h.escape(pick["one_liner"]) + "</p><div class=\"meta\">"
         + ("<span class=\"pick-chip\">" + _h.escape(cat) + "</span>" if cat else "")
         + "<span class=\"pick-chip\">" + _h.escape(price_label) + "</span>"
         "<a class=\"pick-link\" href=\"" + u + "\">noteで読む →</a></div></section>")
    return md, h


def run_todays_pick(items, xposts, today):
    """戻り値 (pick or None, 記録する履歴エントリ or None, 履歴)。例外は呼び出し側で握る"""
    try:
        history = load_pick_history(PICK_HISTORY_PATH)
    except Exception as e:
        print("pick: history file broken (%s: %s) — 枠なし・履歴は上書きしない" % (type(e).__name__, e))
        return None, None, None
    same = next((h for h in history if h.get("date") == today), None)
    if same is not None:
        pick = dict(same["pick"]) if same.get("pick") else None
        if pick:
            pick["linked_url"] = pick_utm_url(pick["url"], pick["article_id"])
            pick["shadow"] = PICK_SHADOW
        print("pick: reuse today's entry (%s)" % (pick["article_id"] if pick else "none"))
        return pick, None, history
    try:
        cands = load_note_candidates(NOTE_MASTER_PATH, today)
    except PickSkip as e:
        print("pick: skip — %s" % e)
        return None, None, history
    ctx = build_pick_context(items, xposts)
    scored, reasons, (adj, nf, npd) = score_note_candidates(ctx, cands, history, today)
    print("pick: candidates=%d eligible=%d cut=%s ratio(last7 free=%d paid=%d → free%+d/paid%+d)"
          % (len(cands), len(scored), json.dumps(reasons), nf, npd, adj, -adj))
    res = choose_todays_pick(scored, today)
    if res is None:
        print("pick: none — no candidate passed (min %d)" % PICK_MIN_SCORE)
        return None, None, history
    pick, top5 = res
    pick["shadow"] = PICK_SHADOW
    for c in top5:
        print("pick: top5 %s %d (rel %d) %s %s" % (c["article_id"], c["score"], c["relevance"],
                                                  json.dumps(c["breakdown"]), c["title"]))
    print("pick: chose %s score=%d shadow=%s %s" % (pick["article_id"], pick["score"], PICK_SHADOW, pick["title"]))
    entry = {"date": today, "shadow": PICK_SHADOW,
             "pick": {k: v for k, v in pick.items() if k not in ("linked_url", "shadow")}, "top5": top5}
    return pick, entry, history


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

    # v1.8: 前日重複の排除（除外前の全候補を state に保存し、直近8日分と照合）
    out_dir = os.environ.get("BRIEFING_OUT", os.path.dirname(os.path.abspath(__file__)))
    history = load_history(today, out_dir)
    candidates = [{"title": it["title"], "link": it["link"], "source": it["source"]} for it in items]
    past_urls, past_titles, past_tokens, past_flags = set(), set(), [], []
    for h in history:
        for c in h.get("candidates", []):
            past_urls.add(url_key(c["link"]))
            past_titles.add(normalize(c["title"]))
            past_tokens.append(token_set(c["title"]))
        past_flags.extend(h.get("flags", []))
    kept, n_same, n_followup = [], 0, 0
    for it in items:
        if url_key(it["link"]) in past_urls or normalize(it["title"]) in past_titles:
            n_same += 1
            continue
        tt = token_set(it["title"])
        if any(overlap(tt, pt) >= FOLLOWUP_RATIO for pt in past_tokens):
            it["followup"] = True
            n_followup += 1
        kept.append(it)
    items = kept

    for it in items:
        cat, sc = classify(it["title"])
        if it.get("force_cat"):
            cat = it["force_cat"]
            sc = max(sc, 4)
        it["category"], it["score"] = cat, min(sc, 8) + it.get("score", 0)
        if any(w in it["title"] for w in PR_WORDS):
            it["score"] -= 4
        ang, ang_base = neta_angle(it["title"])
        if ang:
            it["neta_candidate"] = ang
            it["neta_base"] = ang_base
    from collections import Counter as _C
    _used = _C()
    n_neta_suppressed = 0
    for it in sorted(items, key=lambda x: -x.get("score", 0)):
        ang = it.get("neta_candidate")
        _min = NETA_MIN_SCORE_BASE if it.get("neta_base") else NETA_MIN_SCORE
        if ang and it.get("score", 0) >= _min and _used[ang] < 2:
            # v1.8: 同一角度＋語の重なり50%以上＋新しい材料なし → 2日目以降は旗を立てない
            tt = token_set(it["title"])
            if (not any(w in it["title"] for w in NEW_MATERIAL_WORDS)
                    and any(f.get("angle") == ang and overlap(tt, token_set(f.get("title", ""))) >= FOLLOWUP_RATIO
                            for f in past_flags)):
                it["neta_suppressed"] = True
                n_neta_suppressed += 1
                continue
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

    # v1.9: 今日の1本（掲載対象確定後の独立処理。失敗しても本体は止めない）
    pick, pick_entry, pick_history, pick_md, pick_html = None, None, None, [], ""
    try:
        pick, pick_entry, pick_history = run_todays_pick(items, xposts, today)
        if pick and not pick.get("shadow"):
            pick_md, pick_html = render_pick(pick)
    except Exception as e:
        pick, pick_entry, pick_md, pick_html = None, None, [], ""
        print("pick: error %s: %s — 枠なしで継続" % (type(e).__name__, e))

    if "--polish" in sys.argv:
        polish(items)
    lines = ["# デジタル地下室ブリーフィング（v1.9）― " + today, ""]
    lines += pick_md
    for cat, _ in CATEGORIES + [("その他", [])]:
        group = [i for i in items if i["category"] == cat and i.get("score", 0) > 0 and not i.get("related_to")][:5]
        if not group:
            continue
        lines.append("## " + cat)
        for i in group:
            dup = "（%d媒体）" % i["dup_count"] if i.get("dup_count", 1) > 1 else ""
            fu = "［続報］" if i.get("followup") else ""
            lines.append("- " + fu + "**" + i["title"] + "**" + dup + "（" + i["source"] + "）")
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
    lines.append("前日重複除外: %d件／続報: %d件" % (n_same, n_followup))

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
           ".fu{display:inline-block;margin-right:8px;background:#2b2107;border:1px solid #e8b64c;color:#e8b64c;font-size:11px;padding:0 7px;border-radius:20px;font-family:monospace}"
           ".rel{font-size:12px;color:#8b887a;margin-top:6px}.rel a{color:#a9a695}"
           ".pick{margin-top:16px;background:#1d2130;border:1px solid #e8b64c;border-radius:12px;padding:16px 18px}"
           ".pick .k{color:#e8b64c;font-family:monospace;font-size:12px;letter-spacing:.2em}"
           ".pick h2{font-size:18px;line-height:1.55;margin:8px 0 6px;color:#fff}"
           ".pick h2 a{color:#fff;text-decoration:none}"
           ".pick p{font-size:14px;line-height:1.75;color:#cfcaba;margin:0 0 10px}"
           ".pick .meta{display:flex;flex-wrap:wrap;align-items:center;gap:8px}"
           ".pick-chip{display:inline-block;background:#151822;border:1px solid #2a2e3a;color:#cfcaba;font-size:12px;padding:2px 10px;border-radius:20px}"
           ".pick-link{margin-left:auto;color:#e8b64c;font-size:13.5px;text-decoration:none;font-weight:bold}"
           "@media(max-width:699px){.pick-link{margin-left:0;flex-basis:100%}}"
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
        if i.get("followup"):
            s += "<span class=\"fu\">続報</span>"
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
        if top_item.get("followup"):
            t += "<span class=\"fu\">続報</span>"
        t += "<span class=\"src\">" + esc(top_item["source"]) + "</span>"
        if top_item.get("neta"):
            t += "<br><span class=\"neta\">📌 ネタ候補: " + esc(top_item["neta"]) + "</span>"
        t += "</section>"
        parts.append(t)
    if pick_html:
        parts.append(pick_html)
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
        json.dump({"rss": items, "x": xposts, "todays_pick": pick}, f, ensure_ascii=False, indent=1)
    if pick_entry is not None:   # v1.9: 出力の書き込み成功後にだけ選定履歴を確定
        try:
            if commit_pick_history(PICK_HISTORY_PATH, pick_history, pick_entry, today):
                print("pick: recorded %s to %s" % (pick_entry["pick"]["article_id"], PICK_HISTORY_PATH))
        except Exception as e:
            print("pick: history write failed %s: %s" % (type(e).__name__, e))
    flags = [{"title": i["title"], "link": i["link"], "angle": i["neta"], "date": today} for i in items if i.get("neta")]
    save_state(today, candidates, flags)
    n_cand = len(candidates)
    print("dedupe: candidates=%d same_excluded=%d followup=%d overlap_total=%d (%.1f%%) neta_flags=%d neta_suppressed=%d history_days=%s"
          % (n_cand, n_same, n_followup, n_same + n_followup, 100.0 * (n_same + n_followup) / max(1, n_cand),
             len(flags), n_neta_suppressed, ",".join(h.get("date", "?") for h in history) or "none"))
    print("wrote %s (rss=%d, x=%d)" % (out_md, len(items), len(xposts)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
