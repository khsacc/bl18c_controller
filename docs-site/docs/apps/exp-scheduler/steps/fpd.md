---
sidebar_position: 4
title: FPD (Rad-icon2022)
---

# FPD (Rad-icon2022)

Rad-icon 2022 フラットパネル検出器で XRD 画像を撮影するステップです。

- XRD 撮影の前には、[microscope_out_and_fpd_in](stage.md#microscope_out_and_fpd_in) で XRD 測定モードになっている必要があります。XRD測定モードになっているかどうかは、Ch8とCh9の値で判定されます。

## take_xrd

- XRD 画像を 1 枚撮影し、実行フォルダの `XRD/` に保存します。画像にはステージ位置（Ch3/4/5/7/9）、ガス圧、温度がメタデータとして記録され、`xrd_master.csv` に 1 行追記されます。
- ファイル名は `<prefix>_<suffix…>_<binning>.tif` です。suffix（ガス圧、温度、日時、通し番号など）は XRD Settings で選択し、ステップごとには変更できません。同名のファイルがある場合は末尾に `_2`, `_3`, … が付きます。


### 設定項目

| 設定項目 | 内容 |
|---|---|
| Use Global Settings | チェックしたままなら、撮影条件とオシレーションはすべて Global settings の **XRD Settings** の値を使います |
| File prefix override | 保存ファイル名の prefix。空欄なら XRD Settings の prefix を使います |

**Use Global Settings** のチェックを外すと、次の二つを個別に変更できます（少なくとも一方を選ぶ必要があります）。

**Change Acquisition Settings**（撮影条件）

| 設定項目 | 内容 |
|---|---|
| Exposure | 露光時間（ms） |
| Save to file | 画像をファイルに保存するか |
| Flip | 画像の上下（Vertical）・左右（Horizontal）反転 |
| Dark / Dark file | 暗画像補正の有無と、暗画像ファイル（.tif） |
| Defect / Defect file | 欠陥画素補正の有無、カーネルサイズ（3×3〜6×6）と欠陥ファイル（.txt） |

**Change Oscillation Settings**（Ch11 揺動）

| 設定項目 | 内容 |
|---|---|
| Oscillation | チェックすると、露光中に Ch11 回転ステージを揺動させます |
| Pos (deg) | 往復の端点 A、B（度） |
| Dwell / Speed | 各端点での待機時間と、移動速度（H / M / L） |

揺動を有効にした場合、露光が終わると Ch11 を停止し、θ = 0° に戻してから次のステップへ進みます。端点が移動制約に反する場合は、露光を始めずにエラーになります。



### 撮影時に自動で行われること

- Global settings の **X-ray Settings** で **Only open shutter during XRD measurements** を有効にしている場合、撮影の直前にシャッターを開け（Out）、撮影と保存が終わったら閉じます（In）。
- 同じ欄の **Stop sequence on potential beam dump** が有効な場合（既定）、撮影の前後でリングの状態と電流値を確認し、ビームダンプの可能性があればシーケンスを停止します。
- Instant 1D reduction を有効にしている場合、撮影後に 1D パターンを作成して保存します。

