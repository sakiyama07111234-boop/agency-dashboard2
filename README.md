# 代理店案件ダッシュボード

代理店募集サイトから案件情報を自動収集し、ダッシュボードで一覧・検索・絞り込みできるツール。

## 対象サイト
- b-seeds.com（代理店ドットコム）
- dairitenboshu.com（代理店本舗）
- kkhashi.com（カケハシ）
- fc-hikaku.net（フランチャイズ比較ネット）

## 使い方

### スクレイパー実行
```bash
pip install requests beautifulsoup4
python3 agency_scraper.py
```

### ダッシュボード
`public/index.html` をブラウザで開く。  
同じフォルダに `agency_jobs_latest.csv` があれば自動読み込み。

### Vercelデプロイ
`public/` ディレクトリをVercelのOutput Directoryに指定。  
CSVは `public/` 内に配置すると自動読み込みされる。
