---
sidebar_position: 7
title: 自動測定で作成されるファイル
---

# 自動測定で作成されるファイル

1回の実行で作成されるファイルは、すべて1つのフォルダにまとめて保存されます。

```
<Data directory>/<Run name>_<YYYYMMDD_HHMMSS>/
├── XRD/
├── spectrum/
├── photo/
└── log/
```

- **Data directory** と **Run name** は Logging 欄で設定します。Data directory が空欄の場合は `apps/exp_scheduler/__localdata/runs/` に保存されます。
- 同名のフォルダが既にある場合は、末尾に `_2`, `_3`, … が付きます。
- `XRD/`, `spectrum/`, `photo/` は、該当する測定を保存したときにだけ作成されます。

## `XRD/`

| ファイル | 内容 |
|---|---|
| `<prefix>_<suffix…>_<binning>.tif` | XRD 画像。ステージ位置・圧力・温度などのメタデータ付き |
| `<画像名>_<単位>.csv` など | 1D プロファイル。Instant 1D の自動保存が有効な場合のみ |
| `xrd_master.csv` | 画像1枚につき1行。開始・終了時刻、ステージ位置 (Ch3/4/5/7/9)、開始・終了時の圧力と温度 |
| `IPAnalyzer_analysis_macro_<フォルダ名>.mcr` | `xrd_master.csv` の画像を一括積分する IPAnalyzer マクロ。画像を保存するたびに更新 |

ファイル名の suffix は設定で選択します (`Pgas_0.80MPa`, `Tset_300.00K`, `TB_299.95K`, 日時, 4桁の通し番号)。装置が未接続、または値が古い場合、その suffix は付きません。

## `spectrum/`

ルビー蛍光スペクトルを1回取得するごとに、同じ名前 `<prefix>_<suffix…>_attempt_NN` で3つのファイルが保存されます。

| ファイル | 内容 |
|---|---|
| `.npz` | 全データ (スペクトル、フィット結果、ステージ位置、探索結果、圧力計算結果) |
| `.csv` | スペクトルとフィット曲線。取得条件は `#` で始まるヘッダ行に記載 |
| `.png` | フィット結果のプロット |
| `ruby_master_all_files.csv` | 取得1回 (品質基準を満たさなかった試行も含む) につき1行。ピーク位置、R²、圧力、温度など |
| `ruby_master_summary.csv` | `take_spectrum()` の実行1回につき1行。先頭に終了状態 (`completed` / `stopped` / `failed`) と全ルビーの平均値 (圧力は標準偏差も) を記載し、その後に各ルビーで品質基準を満たした試行のデータを記載。品質基準を満たす試行が無かったルビーの列は空欄 |

## `photo/`

`save_snapshot` で撮影したカメラ画像 `snapshot_<日時>.png` が保存されます。

## `log/`

| ファイル | 内容 |
|---|---|
| `sequence.json` | 実行したシーケンス。**Load Sequence** でそのまま読み込めます |
| `metadata.json` | 開始時刻、記録対象の装置、Global limits、シーケンス |
| `conditions.csv` | 温度・圧力・ステージ位置の時系列。一定間隔 (既定 30 秒) と、XRD 撮影・エラーなどのイベント時に1行追加 |
| `ops.log` | 装置へのコマンドとイベントの全記録 (デバッグ用) |
