---
sidebar_position: 2
title: Interactive camera
---

# Interactive camera

メインウィンドウのボタン：**Interactive camera**

:::note
このページでは、アプリケーションの使用方法に絞って説明します。オートフォーカスおよび自動追従に用いている画像認識手法の詳細については、[Interactive camera で用いられている画像認識手法](https://github.com/khsacc/bl18c_controller/blob/main/docs/DOC_SUB_CAMERA_CV2.md) を参照してください。カメラの較正手順は [Interactive Camera の較正](../../startup/interactive-camera.md) にあります。
:::

## オートフォーカス中の表示

**Interactive Camera** タブでオートフォーカス（Ch3 または Ch7）を実行している間は、ライブ映像の中央に白文字で **Auto-focus in progress...** と表示されます。完了または停止すると表示は消えます。この表示は保存する画像や動画には入りません。

## 試料追従タブ

試料追従には 2 つのタブがあります。同時に動かせるのはどちらか一方だけで、追従中はもう一方のタブと **Interactive Camera** タブは使えません。

| タブ | 方式 |
|------|------|
| **Sample Tracking (Gasket Detection)** | ガスケット穴の輪郭（楕円）を検出し、登録した試料位置を X 線ビーム位置へ動かします。Experimental Scheduler の **Gasket outline (Beta)** と同じ検出・補正処理を使います。 |
| **Sample Tracking (Similarity)** | 参照写真との画像類似度で追従します（従来の方式）。 |

### Sample Tracking (Gasket Detection)

事前に、カメラの較正と X 線ビーム位置の登録（[XRD 測定準備](../../startup/xrd-standard.md)）を済ませてください。

1. **Set up sample positions…** を押し、Experimental Scheduler と同じダイアログで次を行います。
   1. ステージを止めた状態で参照写真を撮影します。
   2. **Mark gasket outline (Beta)…** でガスケット穴の外縁に楕円を合わせ、試料位置（ガスケット中心、または写真上で指定した点）を決めます。
   3. 必要であれば、ルビー位置を登録します。ルビー位置はガスケット中心からのオフセットとして保存されるだけで、このタブではルビー測定を行いません。
2. **Activate gasket detection** を押すと、ステージを動かさずにライブ画像上で検出結果を確認できます。シアンが登録した輪郭、緑が検出した輪郭、マゼンタが試料位置、赤が X 線ビーム位置です。
3. 必要に応じて設定を変更し、**Start Sample Position Tracking by moving Ch3,4,5** を押します。開始直後に 1 回補正し、その後は各回の補正が終わってから **Interval** だけ待って次の補正を行います。**Stop Tracking** を押すと、実行中のステージ移動が終わってから停止し、PM16C を LOC に戻します。

:::warning[重要]
停止時にはステージが止まったことを確認します。確認できない場合は通常停止を送って再確認し、それでも確認できないときは、Interactive Camera がステージの操作権を保持したまま警告を表示します。このとき他のアプリからステージを動かせず、ウィンドウも閉じられません。PM16C の状態を確認してから、**Stop Tracking** の位置に表示される **Confirm stage stopped** を押してください。停止が確認できると操作権を解放します。
:::

| 設定 | 内容 |
|------|------|
| **Gasket tolerance** | 補正後に許容するガスケット中心のずれ（µm）。各回の最初の補正は、ずれが 1 パルス以上あれば許容範囲内でも行います。 |
| **Average frames** | 1 回の測定で平均する新しいフレーム数（1–10）。過半数のフレームで検出が一致しない場合は補正しません。 |
| **Allow uniform ellipse scaling** | 縦横比を保ったまま、穴径の ±20% までの変化を許します。 |
| **XY retries** | 最初の補正の後、許容範囲に入るまで XY を再補正する最大回数。 |
| **Ch3 autofocus …** | XY 補正の前後に Ch3 のオートフォーカスを行います。走査範囲などは **Interactive Camera** タブのオートフォーカス設定を使います。 |
| 移動量の上限 | 1 回あたりの上限（Ch4/Ch5）と、開始位置からの合計の上限（Ch3/Ch4/Ch5）。合計の上限に達すると追従を停止します。 |

検出に失敗した場合、または補正後もずれが **Gasket tolerance** を超える場合は、追従を停止して理由をログに表示します。各回の結果は **Log directory** の `gasket_tracking_log_from_<開始時刻>.csv` に記録され（ステージ位置、ガスケット中心のずれ、検出スコア、接続中の試料環境の値）、停止時に Ch3/Ch4/Ch5 の位置のグラフを保存します。

このタブの設定（参照写真、輪郭、試料位置、ルビー位置を含む）は Interactive Camera 内に保存され、Experimental Scheduler のシーケンス設定とは独立しています。Experimental Scheduler で同じ設定を使うには、Scheduler 側の **Import from Interactive Camera** を押します（[試料追従の設定](../exp-scheduler/sample-tracking.md)）。

## Experimental Scheduler 実行中の動作

ランチャーから起動した場合、顕微鏡カメラへの接続はアプリケーション起動時に始まり、Interactive Camera、Experimental Scheduler、Run Monitor が同じライブ映像を使用します。

シーケンス開始時に Interactive Camera が開いていれば、ウィンドウはそのまま残り、ライブ映像だけを表示する画面に切り替わります。操作メニュー、ステージ移動、撮影、追従、オートフォーカスは使用できず、シーケンス終了後に元の画面へ戻ります。シーケンス中に初めて Interactive Camera を開いた場合も、閲覧専用のライブ画面が表示されます。

:::warning[重要]
録画、追従、オートフォーカス、手動のステージ移動のいずれかが進行中の場合は、それらを終了するまでシーケンスを開始できません。
:::
