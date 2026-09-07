# PDIndexer 連携（Instant 1D → PDIndexer 送信）

Rad-icon 2022 の Instant 1D reduction (XRD) パネルで計算した1次元プロファイルを、
IPAnalyzer を経由せずに直接 PDIndexer へ送信する機能です。

## 使い方

Rad-icon 2022 ウィンドウの **Instant 1D reduction (XRD)** パネルを開くと、
下部に PDIndexer 送信用の行が追加されています。

- **Send to PDIndexer**（チェックボックス）— ONにすると、単発撮影（Take snapshot）
  およびシーケンス撮影の各フレームが完了するたびに、自動的にPDIndexerへ送信されます。
  ライブビュー中、および露光時間・poniファイルの変更による再計算では送信されません。
  既定は OFF です。
- **via:**（プルダウン）— 送信経路を選択します。
  - **Clipboard** — Windowsクリップボード経由（要 `PdiSender.exe`、Windows専用）。
  - **.pdi folder (…)** — PDIndexerのファイル監視機能を使う経路。表示されているパスに
    `.pdi` ファイルが書き出されます。事前にPDIndexer側で、このパスを監視対象ディレクトリに
    設定し、「新規プロファイルの自動読み込み」を有効にしておく必要があります。
  - 利用できない経路は選択肢に表示されません（両方とも利用できない場合はグレーアウトします）。
- **Send now**（ボタン）— チェックボックスの状態に関わらず、現在表示されている
  1次元プロファイルを直ちに1回だけ送信します。

送信結果はボタンの右側のラベルに表示されます。

> [!IMPORTANT]
> PDIndexerは送信の受信確認（ACK）を返しません。「送信しました」という表示は
> 「クリップボードへの書き込み（またはファイルの書き出し）に成功した」ことを意味するのみで、
> PDIndexer側で実際にプロファイルリストに追加されたかどうかは、PDIndexerの画面を直接
> 確認してください。

> [!NOTE]
> **Send to PDIndexer** を有効にすると、撮影のたびにクリップボードの内容が上書きされます
> （Clipboard経由を選択している場合）。他の作業でクリップボードを使う予定がある場合は
> ご注意ください。

## Clipboard経由を使うための準備（Windows）

1. `utils/pdindexer/csharp/build.bat` を実行し、`utils/pdindexer/bin/PdiSender.exe`
   をビルドします（要 .NET SDK。ビルド時のみ必要で、実行時には不要です）。
2. PDIndexerを起動しておきます。
3. 上記の **via:** から **Clipboard** を選択します。

`PdiSender.exe` が存在しない場合、または非Windows環境では、Clipboard経由は
選択肢に表示されません。

## .pdi folder経由を使うための準備

1. PDIndexerのツールバーで、監視するディレクトリを指定します。
   既定のパスは起動方法によって異なります。
   - `python main.py` から起動した場合（通常の起動方法）:
     リポジトリ直下の `__localdata/pdindexer_watch/`
     （Rad-icon 2022 と XRD Scan が同じ `PdiService` を共有するため、共通の1か所になります）。
   - Rad-icon 2022 を単独で起動した場合:
     `apps/Rad_icon_2022/__localdata/pdindexer_watch/`
   - XRD Scan を単独で起動した場合:
     `apps/xrd_scan/__localdata/pdindexer_watch/`
     （単独起動時は各ウィンドウ／ダイアログが自前の `PdiService` を持つため、
     アプリごとに別のディレクトリになります）。

   実際のパスは **via:** プルダウンの `.pdi folder (…)` 項目に表示されます。
2. 「新規プロファイルの自動読み込み」を有効にします。
3. 上記の **via:** から **.pdi folder** を選択します。

## 技術的な背景

この機能の設計判断・上流ソフトウェア（IPAnalyzer / PDIndexer / Crystallography）の
仕様調査結果は
[docs/PLAN_PDINDEXER_BRIDGE.md](PLAN_PDINDEXER_BRIDGE.md) に、
実装の技術的詳細（バイナリフォーマット・単位変換など）は
[utils/pdindexer/IMPLEMENTATION_DETAILS.md](../utils/pdindexer/IMPLEMENTATION_DETAILS.md)
にまとめてあります。開発者はこちらを参照してください。
