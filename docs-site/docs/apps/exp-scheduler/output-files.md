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

Run name は実行フォルダーとすべての実行ファイルに共通の prefix です（フォルダーの日時はファイルの prefix に含めません）。空欄は `run` とし、前後の空白は除去します。Windows で使えない文字・予約名・末尾のドットは使用できません。長さは UTF-16 で80単位までです。

独自の File prefix / File prefix override 入力欄は廃止しました。旧シーケンスの `prefix` 設定・旧DSLの `prefix=` 引数は読み込めますが、保存名には使いません。新しいJSON・DSLには独自prefixを出力しません。連番・suffix・binning・再試行番号・同名回避の規則は変更していません。suffix が空なら、その部分の区切りも省略します。追跡用参照画像 (`save_reference_image`) は指定パスを維持します。

## `XRD/`

| ファイル | 内容 |
|---|---|
| `<Run name>_XRD_<suffix…>_<binning>.tif` | XRD 画像。ステージ位置・圧力・温度などのメタデータ付き |
| `<画像名>_<単位>.csv` など | 1D プロファイル。Instant 1D の自動保存が有効な場合のみ |
| `<Run name>_xrd_master.csv` | 画像1枚につき1行。開始・終了時刻、ステージ位置 (Ch3/4/5/7/9)、開始・終了時の圧力と温度 |
| `<Run name>_IPAnalyzer_analysis_macro.mcr` | `<Run name>_xrd_master.csv` の画像を一括積分する IPAnalyzer マクロ。画像を保存するたびに更新 |

ファイル名の suffix は設定で選択します (`Pgas_0.80MPa`, `Tset_300.00K`, `TB_299.95K`, 日時, 4桁の通し番号)。装置が未接続、または値が古い場合、その suffix は付きません。

## `spectrum/`

ルビー蛍光スペクトルを1回取得するごとに、同じ名前 `<Run name>_spectrum_<Ruby name>_<suffix…>_attempt_NN` で3つのファイルが保存されます。

| ファイル | 内容 |
|---|---|
| `.npz` | 全データ (スペクトル、フィット結果、ステージ位置、探索結果、圧力計算結果) |
| `.csv` | スペクトルとフィット曲線。取得条件は `#` で始まるヘッダ行に記載 |
| `.png` | フィット結果のプロット |
| `<Run name>_ruby_master_all_files.csv` | 取得1回 (品質基準を満たさなかった試行も含む) につき1行。ピーク位置、R²、圧力とその誤差 (合計・内訳)、温度など |
| `<Run name>_ruby_master_summary.csv` | `take_spectrum()` の実行1回につき1行。先頭に終了状態 (`completed` / `stopped` / `failed`) と全ルビーの平均値 (圧力は不確かさと標準偏差も) を記載し、その後に各ルビーで品質基準を満たした試行のデータを記載。品質基準を満たす試行が無かったルビーの列は空欄 |


ルビー名には **Set up sample positions…** で登録した位置の名前を使います。
全位置測定・自動加減圧でも各測定のルビー名が入ります。ファイル名内では
Windows禁止文字を `_` に置換し、前後の空白・ドットを除き、UTF-16で40単位までに
短縮します。空になる場合は `ruby` を使います。置換・短縮で同名になっても既存の
重複回避処理で上書きを防ぎます。NPZ・CSV・マスターCSVには元のルビー名を保持します。

### 複数ルビーの平均圧力と不確かさ

`<Run name>_ruby_master_summary.csv` の平均圧力 `pressure_mean_GPa` は、圧力が有効 (`pressure_status` が `valid`) なルビーの単純平均です。
その不確かさは、FluoRaPressée (FRP) が各ルビーについて返す圧力の誤差を次の2つの成分に分けて伝播させて求めます。

| 成分 | 各ルビーの列 | 由来 | ルビー間の関係 |
|---|---|---|---|
| ピーク由来 $\sigma_{\mathrm{peak},i}$ | `pressure_error_peak_GPa` | フィットで得た R1 ピーク位置の誤差 | 独立 |
| スケール由来 $\sigma_{\mathrm{scale},i}$ | `pressure_error_scale_GPa` | 圧力スケールの係数の誤差 (Shen 2020、Holzapfel 2003、Jacobsen 2008 など。係数の誤差がないスケールでは 0) | 同じスケールを使うので完全に相関 |

各ルビーの `pressure_error_GPa` は、この2つの二乗和の平方根です。
有効なルビーが $n$ 個のとき、平均と不確かさは次のとおりです。

$$
\bar{P} = \frac{1}{n}\sum_{i=1}^{n} P_i,\qquad
\sigma_{\mathrm{peak}}(\bar{P}) = \frac{1}{n}\sqrt{\sum_{i=1}^{n} \sigma_{\mathrm{peak},i}^2},\qquad
\sigma_{\mathrm{scale}}(\bar{P}) = \frac{1}{n}\sum_{i=1}^{n} \sigma_{\mathrm{scale},i}
$$

$$
\sigma(\bar{P}) = \sqrt{\sigma_{\mathrm{peak}}(\bar{P})^2 + \sigma_{\mathrm{scale}}(\bar{P})^2}
$$

それぞれ `pressure_mean_error_peak_GPa`、`pressure_mean_error_scale_GPa`、`pressure_mean_error_GPa` に記録します。

- ピーク由来の成分はルビーの数を増やすと $1/\sqrt{n}$ で小さくなりますが、スケール由来の成分は平均しても小さくなりません。
- スケール由来の成分を完全に相関するものとして扱うのは、係数が1つのスケールでは厳密で、係数が複数あるスケールでは上限 (安全側) の見積もりです。
- λ₀ の不確かさと、温度補正 (補正式・温度測定) の不確かさは含まれません。
- ルビーごとの圧力のばらつき (圧力勾配など) もこの不確かさには含まれません。ばらつきは標本標準偏差 `pressure_sd_GPa` として別に記録します。ばらつきから見た平均の標準誤差は `pressure_sd_GPa` / $\sqrt{n}$ ($n$ = `pressure_valid_count`) です。
- 有効なルビーのうち1つでも誤差の内訳が得られなかった場合 (内訳を返さない古いバージョンの FRP を使った場合など) は、不確かさの3列を空欄にします。平均値と標準偏差は通常どおり記録します。

## `photo/`

`save_snapshot` で撮影したカメラ画像 `<Run name>_snapshot_<日時>.png` が保存されます。

## `log/`

| ファイル | 内容 |
|---|---|
| `<Run name>_sequence.json` | 実行したシーケンス。**Load Sequence** でそのまま読み込めます |
| `<Run name>_metadata.json` | 開始時刻、記録対象の装置、Global limits、シーケンス |
| `<Run name>_conditions.csv` | 温度・圧力・ステージ位置の時系列。一定間隔 (既定 30 秒) と、XRD 撮影・エラーなどのイベント時に1行追加 |
| `<Run name>_ops.log` | 装置へのコマンドとイベントの全記録 (デバッグ用) |
| `<Run name>_run_log.pdf` | 実行ステップ一覧。終了・停止・エラー後に保存 |

### PDF の備考欄

GUI の実行ログ表と `<Run name>_run_log.pdf` は、備考がある場合だけ末尾に **Remarks** 列を追加します。

- **XRD**: LakeShore 接続時、Ch B の撮影前後の実測値の平均を `Mean TB: 202.00 K` のように記載します。設定温度ではありません。前後いずれかの値が取得できなければ空欄です。オシレーション撮影では、終了側は回転ステージの停止・原点復帰後の値を使います。
- **通常のルビー測定**: そのステップで品質基準を通過した有効な測定の平均圧力を `Mean ruby pressure: 2.000 GPa` のように記載します。全位置測定では全ルビーの有効値を平均し、再試行の不採用値・無効な圧力は除きます。圧力計算が OFF の場合は空欄です。
- **ルビーを使う加減圧**: 絶対圧・相対圧の加圧／減圧ステップでは、途中の圧力を平均せず、最後に品質基準を通過した有効な測定値を `Last ruby pressure: 2.000 GPa` のように記載します。
- 測定ファイルの保存が OFF でも記載します。途中停止・エラー時は、通常測定では取得済みの有効な圧力の平均、加減圧ステップでは最後の有効測定値を残します。有効測定値がなければ空欄です。
- 全行に備考がなければ、従来どおり備考列なしで出力します。CSV の形式は変わりません。
