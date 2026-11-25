---
sidebar_position: 3
title: X-ray shutter
---

# X-ray shutter

ハッチ内、イオンチャンバーの下流にある簡易シャッターを開け閉めします。動作には、右側のコンピュータの電源がONになっていることが必要です。

![X-ray shutter アプリのスクリーンショット](/img/apps/xrd/shutter-control.png)

## Manual Control / Auto Control

起動時は **Manual Control** です。従来どおりボタンで IN（遮断）／OUT（照射）を切り替えます。

同じランチャーから開いた Keithley を使う透過スキャン（1D / 2D Scan、DAC Scan (Normal / Rotation Centre)、Collimator Scan）では、走査開始前にシャッター状態を確認します。**IN の場合はX線が遮断されていることを警告し、スキャンを開始しません。** 手動で OUT にするか Auto Control に切り替えてから、再度スキャンを開始してください。STARS 通信エラーや移動中などで状態を確認できない場合も、警告して開始を止めます。この確認ではシャッターを動かしません。Ruby Finder などの蛍光測定、および共有シャッターがない standalone 起動には適用されません。判定はシャッター状態に基づき、Keithley の電流値からX線の有無を判定するものではありません。

**Auto Control** に切り替えると、まず IN を確認します。その後は同じランチャーから開いたアプリで、次の測定に合わせてバックエンドが開閉します。

- Keithley を使用する 1D / 2D Scan、DAC Scan (Normal / Rotation Centre)、Collimator Scan：走査中は OUT、走査終了・中断・エラー時は IN。中心位置への復帰前に閉じます。
- Rad-icon：単発、Sequential、Live、XRD Oscillation、および XRD Scan / ROI のテスト撮像中は OUT。終了時は IN。ダーク取得中は IN です。
- Ruby Finder などの蛍光測定はシャッターを開けません。

Auto 中は手動開閉ボタンを無効にします。測定中はモードを変更できず、競合する測定は開始を拒否します。OUT を確認できない場合は撮像・透過測定を開始せず、IN への復帰を試みます。IN を確認できない場合もエラーを表示します。停止時、実行中の検出器読出しはキャンセルできないため、その処理が戻った後に閉じます。

設定はアプリ実行中のみ共有されます。このツールを閉じても Auto は継続し、プログラムの次回起動時は Manual に戻ります。別プロセスで起動した standalone アプリには共有されません。

## Experimental Scheduler との関係

Manual では Scheduler の既存の **Only open shutter during XRD measurements** 設定が独立して動作します。Auto では、そのチェック状態にかかわらず XRD 時のみ開き、待機・ステージ移動・蛍光測定では閉じます。Auto では終了後に初期 OUT 位置へ戻す確認も表示しません。ビームダンプ検出の設定は従来どおり独立です。保存済みシーケンスの設定内容は変更しません。

Auto の Sequential / Live / Dark / XRD Oscillation では、開閉前から露光していた可能性のある最初の FreeRun フレームを 1 枚破棄します。保存枚数は変わりませんが、その分の取得時間が加わります。
