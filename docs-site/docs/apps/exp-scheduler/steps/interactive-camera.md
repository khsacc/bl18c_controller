---
sidebar_position: 3
title: Interactive Camera
---

# Interactive Camera

## save_snapshot

カメラの新しいフレームを 1 枚取得し、実行フォルダの `photo/` に `snapshot_YYYYMMDD_HHMMSS_mmm.png` として保存します。

## start_following

バックグラウンドで試料追従を開始し、すぐに次のステップへ進みます。追従は [stop_following](#stop_following) を実行するまで続き、一定間隔でリファレンス画像と現在の画像を比較して、ずれを Ch4/Ch5 で補正します。

リファレンス画像、補正間隔、オートフォーカスなどの条件は Global settings の値を使います。

:::warning

start_following に対応する stop_following がないと、Validation で警告が出ます。

:::

## stop_following

試料追従を停止します。追従が実施中に stop_following が呼ばれた場合、その追従操作が完全に終わってから次のステップへ進みます。

## follow_sample_position

指定した時間だけ試料追従を行い、終わったら追従を止めて次のステップへ進みます。start_following → wait → stop_following の 3 ステップをまとめたものです。

| 設定項目 | 内容 |
|---|---|
| Duration | 追従する時間（`min` / `s`） |

追従の条件は Global settings の値を使います。

## align_to_reference

リファレンス画像に合わせて試料位置を **1 回だけ** 補正し、次のステップへ進みます。追従を 1 周期だけ実行するもので、バックグラウンドの処理は残りません。

| 設定項目 | 内容 |
|---|---|
| Run Ch3 autofocus after XY correction | チェックすると、XY 補正の後に Ch3 のオートフォーカスを行います（既定でオン） |

その他の条件は Global settings の値を使います。start_following と stop_following の間では使えません。

## save_reference_image

その時点のカメラ画像をリファレンス画像として保存します。シーケンスの途中で新しいリファレンス画像が必要になった場合に使いますが、滅多に使わないと踏んでいます。

| 設定項目 | 内容 |
|---|---|
| Save path | 保存先。**Use Global Settings** をチェックしたままなら既定の場所（`__localdata/reference_frame.png`）に保存します |
| Camera index | 使用するカメラの番号（通常は 0） |

