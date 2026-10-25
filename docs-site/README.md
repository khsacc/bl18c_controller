# BL-18C Controller Manual

BL-18C Controller のオンラインマニュアルを生成する [Docusaurus](https://docusaurus.io/) プロジェクトです。
公開先: https://khsacc.github.io/bl18c_controller/

## セットアップ

```bash
npm ci
```

Node.js 20 以上が必要です。nodenv を使用している場合は、このディレクトリの `.node-version` により互換性のある Node.js が自動的に選択されます。

## ローカル開発

```bash
npm run start
```

`http://localhost:3000/bl18c_controller/` でプレビューできます。

## ビルド

```bash
npm run typecheck
npm run build
```

生成物は `build/` に出力されます。`main` ブランチへの push 時（`docs-site/**` に変更がある場合）には、
リポジトリルートの GitHub Actions（`.github/workflows/deploy-docs.yml`）から GitHub Pages へ自動公開されます。

## 構成

- `docs/intro.mdx` — ランディングページ（サイトルート `/`）
- `src/components/DocList.tsx` — カテゴリのページ一覧を箇条書きで表示する部品（`startup/index.md` で使用）
- `src/css/custom.css` — 配色などのグローバルスタイル
- サイドバーは `docs/` のフォルダ構成から自動生成されます（`sidebar_position` と `_category_.json` で順序を指定）。
- フォルダと同名のファイル（例: `apps/scan/scan.md`）は Docusaurus がそのフォルダの index ページとみなし、`index.md` と衝突して警告なしに出力されなくなります。その場合はフロントマターに `slug: <ファイル名>` を書いてください。
- 画像は `static/img/` 以下に置き、本文からは `/img/...` で参照します（`baseUrl` は自動で付きます）。
