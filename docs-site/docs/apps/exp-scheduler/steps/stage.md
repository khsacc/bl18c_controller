---
sidebar_position: 2
title: Stage
---

# Stage

どの操作でも、ステージ間の移動制約（Ch9 と Ch8 、Ch8 と Ch11）や Global Limits に反する移動はエラーになります。

## microscope_out_and_fpd_in

**XRD 測定モード** への切り替え：顕微鏡（Ch8）を OUT 位置へ退避させ、移動が終わってから検出器（Ch9）を IN 位置へ入れます。

| 設定項目 | 内容 |
|---|---|
| Speed | 移動速度（H / M / L） |
| Microscope OUT pos | Ch8 の OUT 位置。**Use global Positions** をチェックしたままなら、Global settings の Positions の値を使います |
| FPD IN pos | Ch9 の IN 位置。同上 |

## fpd_out_and_microscope_in

**顕微鏡観察モード** への切り替え：検出器（Ch9）を OUT 位置へ退避させ、移動が終わってから顕微鏡（Ch8）を IN 位置へ入れます。

| 設定項目 | 内容 |
|---|---|
| Speed | 移動速度（H / M / L） |
| FPD OUT pos | Ch9 の OUT 位置。**Use global Positions** をチェックしたままなら、Global settings の Positions の値を使います |
| Microscope IN pos | Ch8 の IN 位置。同上 |


## move_absolute

指定したチャンネルを、指定した位置（絶対値）へ移動します。移動が完了するまで次のステップには進みません。

| 設定項目 | 内容 |
|---|---|
| Channel | 1〜11 |
| Position (pulses) | 移動先のパルス数。For ループ内ではループ変数も使えます |
| Speed | **Keep current speed** をチェックしたままなら現在の速度のまま。外すと H / M / L を指定できます |

## move_relative

指定したチャンネルを、現在位置から指定したパルス数だけ移動します。設定項目は move_absolute と同じで、Position の代わりに **Delta (pulses)**（移動量）を指定します。For ループ内ではループ変数も使えます。

## set_speed

指定したチャンネルの移動速度を H / M / L のいずれかに切り替えます。以後、そのチャンネルの移動はこの速度で行われます。

| 設定項目 | 内容 |
|---|---|
| Channel | 1〜11 |
| Speed | H / M / L |

## normal_stop

全チャンネルを減速停止させます。

## emergency_stop

全チャンネルを減速せずに即座に停止させます。
