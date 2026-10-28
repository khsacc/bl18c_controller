---
sidebar_position: 0
title: 各ステップでできること
---

# 各ステップでできること

Visual タブの **+ Add Step** を押すと、ステップの編集ダイアログが開きます。上段の **Device** で装置のグループを選び、**Operation** でそのグループの操作を選ぶと、下に操作ごとの設定欄が表示されます。

| Device | 主な操作 |
|---|---|
| [General](general.md) | 待機、ログへのメッセージ記録 |
| [Stage](stage.md) | XRD／顕微鏡モードの切り替え、各チャンネルの移動、速度設定、停止 |
| [Interactive Camera](interactive-camera.md) | スナップショット保存、試料追従、リファレンス画像への位置合わせ |
| [FPD (Rad-icon2022)](fpd.md) | XRD 画像の撮影 |
| [Ruby Fluorescence](ruby-fluorescence.md) | ルビー蛍光スペクトルの測定 |
| [PACE5000](pace5000.md) | ガス圧の設定・到達待ち、ルビー圧を目標にした加圧・減圧 |
| [LakeShore](lakeshore.md) | 温度の設定・到達待ち、ヒーター設定 |

## 共通の考え方

- **Use Global Settings** などのチェックボックスが付いた欄は、チェックしたままにすると画面左側の Global settings の値を使います。チェックを外すと、そのステップだけ個別の値を指定できます。
- For ループの中にステップを追加すると、一部の数値欄（移動先の位置、圧力、温度など）で **Constant** と **Loop variable** を切り替えられます。Loop variable を選ぶと、ループ変数の値がそのステップで使われます。
- ステップの設定値は、実行前の Validation でまとめて検査されます（[Validationの内容](../validation.md)）。
