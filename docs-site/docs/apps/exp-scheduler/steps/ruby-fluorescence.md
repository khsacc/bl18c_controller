---
sidebar_position: 5
title: Ruby Fluorescence
---

# Ruby Fluorescence

FluoRaPressée と連携して、ルビー蛍光スペクトルを測定するステップです。Global settings の **Ruby Fluorescence Settings** と、**Sample Positions** で登録したルビー位置を使います。

## take_spectrum

登録したルビー位置へステージを移動してルビー蛍光スペクトルを測定し、XRD 位置へ戻ります。

### 実行の流れ

1. 顕微鏡モードであることを確認します（XRD モードではエラー）。試料追従が動作中なら一時停止します。
1. XRD 位置でカメラ画像をリファレンス画像と照合し、Ch4/Ch5 のずれを補正します。
1. 補正後の位置に、登録したルビー位置の差分（Ch3/4/5）を加えた位置へ移動します。
1. （探索を有効にした場合）近傍を探索し、最も強度の高い点へ移動します。
1. スペクトルを取得し、2 本のピークでフィットします。飽和した場合は露光時間を半分にして撮り直し、R² が閾値を下回った場合は EM Gain を上げて撮り直します。
1. XRD 位置へ戻ります。

### 設定項目

| 設定項目 | 内容 |
|---|---|
| Save full spectrum to file | スペクトルを実行フォルダの `spectrum/` に保存するか |
| File prefix | 保存ファイル名の prefix（既定は `spectrum`） |
| Ruby position | 測定するルビー位置（登録済みの名前から選択） |
| Measure all registered ruby positions | チェックすると（既定）、登録したすべてのルビー位置を名前順に測定します。外すと Ruby position で選んだ 1 か所だけを測定します |
| Exposure time | 露光時間（0.01〜5 s）。**Use Global Settings** をチェックしたままなら Global settings の値を使います |
| Maximum EM Gain | 再測定時に上げる EM Gain の上限。同上 |
| Search best position nearby | チェックすると、ルビー位置の周囲を格子状に探索し、最も強度の高い点で測定します |
| Use Global Settings for nearby search | チェックしたままなら、探索条件は Global settings の値を使います |
| Search timing | **Always**：毎回探索します。**If initial R² is below threshold**：まずルビー位置で測定し、R² が閾値を下回った場合だけ探索します |
| Ch3/Ch4/Ch5 full range | 探索に使う軸、探索範囲（全幅、µm）と刻み幅（パルス数） |



### 圧力が規定値に達したら停止する

Ruby Fluorescence Settings の **Pressure Calculation** を有効にしている場合、次の設定が使えます。

| 設定項目 | 内容 |
|---|---|
| Stop sequence when ruby pressure reaches target | チェックすると、ルビー圧が目標に達した時点でシーケンスを終了します |
| Target pressure | 目標圧力（GPa） |
| Tolerance | 許容幅（GPa）。「目標 − 許容幅」以上の有効なルビー圧が得られたら終了します |

PACE5000 で段階的に加圧しながらルビー圧を測るループで、目標圧力に達したら止める、という使い方を想定しています。条件を満たすと、シーケンスはルビー位置で終了します（XRD 位置へは戻りません）。PACE5000 は最後の設定圧のまま Control モードで残るので、その後の圧力の扱いは利用者が判断してください。また、ルビー圧は測定のたびにしか分からないため、ちょうど目標圧で止まる保証はありません。

ルビー圧を目標にして自動で加圧・減圧したい場合は、PACE5000 の [compress_to_target_pressure_using_ruby](pace5000.md#compress_to_target_pressure_using_ruby) を使ってください。
