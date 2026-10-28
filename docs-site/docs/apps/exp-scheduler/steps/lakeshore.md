---
sidebar_position: 7
title: LakeShore
---

# LakeShore

LakeShore 335 温度コントローラーを操作するステップです。温度の単位は K です。

## set_and_wait_temperature

設定温度と昇降温速度を設定し、温度が到達するまで待ってから次のステップへ進みます。set_temperature と wait_temperature を 1 ステップにまとめたもので、通常はこちらを使います。

| 設定項目 | 内容 |
|---|---|
| Temperature (K) | 設定温度。For ループ内ではループ変数も使えます |
| Ramp rate (K/min) | 昇降温速度 |
| Temperature sensor | 到達の判定に使うセンサー（T_A / T_B、既定は T_B） |
| Tolerance (K) | 到達とみなす許容幅 |

選んだセンサーの測定値が「設定温度 ± 許容幅」に入り、かつランプ（設定値の段階的な変化）が終わったら到達とみなします。

## set_temperature

設定温度と昇降温速度を設定し、**到達を待たずに** 次のステップへ進みます。

| 設定項目 | 内容 |
|---|---|
| Temperature (K) | 設定温度。For ループ内ではループ変数も使えます |
| Ramp rate (K/min) | 昇降温速度 |

## wait_temperature

温度が到達するまで待ちます。目標は直前の set_temperature の設定温度（なければ LakeShore に設定されている値）です。

| 設定項目 | 内容 |
|---|---|
| Temperature sensor | T_A / T_B（既定は T_B） |
| Tolerance (K) | 到達とみなす許容幅 |

判定の方法は set_and_wait_temperature と同じです。待機中に LakeShore 側で設定温度が変更された場合はエラーになります。

## set_heater

ヒーターのレンジを切り替えます。

| 設定項目 | 内容 |
|---|---|
| Range | Off / Low / Medium / High |

## all_heaters_off

すべてのヒーター（両チャンネル）をオフにします。
