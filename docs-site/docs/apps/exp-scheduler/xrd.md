---
sidebar_position: 3
title: XRD測定の設定
---

# XRD測定の設定

## Instant 1D reduction のマスク

Global settings → **XRD Settings** → **Instant 1D reduction (XRD)** を有効にし、
**Mask file** の **Browse…** から除外マスクを選択します。
[XRD 1D reduction](../xrd/1d-reduction.md) で保存した `.npz` と、
IPAnalyzer の `.mas` に対応しています。ファイル欄を空にすると適用を解除します。
選択したパスは ES の設定とシーケンス JSON に保存されます。

マスクは、反転・補正後の TIFF と同じ向き・画像サイズで作成してください。
非ゼロ（True）の画素を積分から除外します。`.mas` は寸法を持たないため画素数で確認します。
**Find spots before 1D integration** と併用でき、検出器マスク・スポットマスクと
合わせて適用します。ファイルマスクで除外した画素はスポット検出の統計にも含めません。
保存する TIFF と画像プレビューは変わらず、1D パターンにのみ反映されます。

Validate 時と実行開始前の再検証で、指定ファイルの読込とサイズを確認します。
実行時にも最初の XRD 撮影前に読み込み、シーケンス内ではそのマスクを保持します。
読込失敗やサイズ不一致はエラーになります。解析用 PC などで再実行するときは、
マスクの保存場所と画像のビニングが一致していることを確認してください。

## シャッターの設定

Manual Control では **Only open shutter during XRD measurements** を個別に設定します。
Tools → X-ray shutter で Auto Control を選択している間は、このチェックにかかわらず XRD 時のみ開き、終了後は IN にします。初期 OUT 位置への復元確認は表示しません。ビームダンプ検出は独立した設定です。
詳しくは [X-ray shutter](../xrd/shutter-control.md) を参照してください。
