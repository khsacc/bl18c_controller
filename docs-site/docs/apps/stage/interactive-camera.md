---
sidebar_position: 2
title: Interactive camera
---

# Interactive camera

メインウィンドウのボタン：**Interactive camera**

:::note
このページでは、アプリケーションの使用方法に絞って説明します。オートフォーカスおよび自動追従に用いている画像認識手法の詳細については、[Interactive camera で用いられている画像認識手法](https://github.com/khsacc/bl18c_controller/blob/main/docs/DOC_SUB_CAMERA_CV2.md) を参照してください。カメラの較正手順は [Interactive Camera の較正](../../startup/interactive-camera.md) にあります。
:::

## Experimental Scheduler 実行中の動作

ランチャーから起動した場合、顕微鏡カメラへの接続はアプリケーション起動時に始まり、Interactive Camera、Experimental Scheduler、Run Monitor が同じライブ映像を使用します。

シーケンス開始時に Interactive Camera が開いていれば、ウィンドウはそのまま残り、ライブ映像だけを表示する画面に切り替わります。操作メニュー、ステージ移動、撮影、追従、オートフォーカスは使用できず、シーケンス終了後に元の画面へ戻ります。シーケンス中に初めて Interactive Camera を開いた場合も、閲覧専用のライブ画面が表示されます。

:::warning[重要]
録画、追従、オートフォーカス、手動のステージ移動のいずれかが進行中の場合は、それらを終了するまでシーケンスを開始できません。
:::
