# 代理店案件スクレイパー＋ダッシュボード

## プロジェクト概要
代理店募集サイトから案件情報をスクレイピングし、ダッシュボードで可視化するシステム。
- 公開URL: https://agency-dashboard2.vercel.app
- GitHub: https://github.com/sakiyama07111234-boop/agency-dashboard2

## ファイル構成
- `agency_scraper.py` - スクレイパー本体（Python 3, requests + BeautifulSoup4）
- `index.html` - ダッシュボードUI（単一HTMLファイル、Vercelでホスティング）
- `agency_jobs.db` - SQLiteデータベース（スクレイピング結果）
- `agency_jobs_latest.csv` - CSV出力（ダッシュボードが読み込む）

## 対象サイトとアダプター
| サイト | クラス | URL形式 | 備考 |
|--------|--------|---------|------|
| b-seeds.com | `BSeedsAdapter` | `/{prefix}-{slug}` | 会社名は `p.agency-fv__doc--company` またはテーブル `企業名` |
| dairitenboshu.com | `DairitenboshuAdapter` | `/detail/{id}` | JS描画のためrequestsでは取得不可（要対応） |
| kkhashi.com | `KakehashiAdapter` | `/matters/detail/{id}` | テーブルth/tdから抽出 |
| fc-hikaku.net | `FcHikakuAdapter` | `/{slug}_fc` | 会社名は `p.sub_text` またはテーブル `会社名` |

## 開発コマンド
```bash
# スクレイパー実行
python3 agency_scraper.py

# 依存パッケージ
pip install requests beautifulsoup4
```

## コーディング規約
- Python 3.10+、型ヒント使用
- 日本語コメント
- commitメッセージは日本語OK

## 課題リスト（優先順）
1. ~~b-seedsとfc-hikakuの会社名抽出ロジック改善~~ ✅完了
2. GitHub Actionsで毎日自動スクレイピング→自動commit→Vercel自動デプロイ
3. 成約管理ページの追加（会社名/URL/商材/手数料/支払い時期/戻入条件/備考）
4. dairitenboshuのJS対応（Playwright等）
5. データ活用（新着通知、営業リスト等）
