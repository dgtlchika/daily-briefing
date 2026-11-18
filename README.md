# daily-briefing

A Japanese tech-news briefing generator. Every morning it collects RSS feeds and X posts, dedupes and scores them, groups articles about the same story, flags topics that could become articles, polishes the top summaries with an LLM, and publishes a static briefing page.

**Live output:** https://kohaku.dgtl-base.com/briefing/ (updated daily at 07:00 JST)

日本語の技術ニュース（スマホ・PC・IT資格・AI／セキュリティ）を毎朝自動で集めて、重複排除・スコアリング・同一事件の束ね・ネタ候補フラグ・AI要約までやって、静的ページとして公開するツールです。

## Features

- **Fixed RSS sources** — ITmedia, GIGAZINE, plus Google News RSS for IT exam news
- **Dedupe & scoring** — title normalization, cross-source overlap bonus, keyword classification into 5 categories （セキュリティ / AI・Muse / スマホ・PC / IT資格 / その他）
- **Freshness windows** — 72h by default, 7 days for sparse exam news
- **Same-story grouping** — related articles are nested under the main one
- **📌 Article-idea flags** — topics that could become articles, with an angle; per-angle caps keep them from clustering on one theme
- **AI summary polish (optional)** — top items are rewritten into one-sentence summaries via the Claude Code CLI
- **X observation** — keyword search via FxEmbed (unofficial, read-only), filtered by engagement thresholds and spam patterns
- **Static HTML output** — dark card-based template with a TOP STORY hero; Markdown output included

## Requirements

- Python 3.10+ (standard library only)
- `curl` (used for fetching)
- Optional: [Claude Code](https://claude.com/product/claude-code) CLI for `--polish`
- `fx2.py` (bundled) for the X observation section

## Usage

```bash
python3 brief.py            # writes briefing-YYYY-MM-DD.md/.html + index.html
python3 brief.py --polish   # also polish top summaries with the Claude Code CLI
```

Set `BRIEFING_OUT` to change the output directory.

### Run it every morning (systemd user timer)

```ini
# ~/.config/systemd/user/briefing.timer
[Timer]
OnCalendar=*-*-* 07:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

Pair it with a oneshot service that runs `brief.py --polish` in the repo directory.

## Development log

The full build history (v0 hand-made prototype → v1.7 designed template), including what failed and what the measurements showed, is in [PROCESS.md](PROCESS.md) — in Japanese.

## Notes

- The X observation uses an unofficial API. Keep the frequency low and use it at your own risk.
- Exam-news feeds are noisy by nature; non-IT exams are filtered out unless an IT anchor word is present.

## License

MIT
