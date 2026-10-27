---
sidebar_position: 1
title: FPD + Scope
---

# FPD + Scope

メインウィンドウのボタン：**Microscope + FPD stage control**

X 線検出器ステージおよび顕微鏡ステージの移動を制御するアプリケーションです。

![FPD and scope stage controller のスクリーンショット](/img/apps/stage/screenshot_fpd_scope_stg.jpg)

## 機能紹介

### Detector (Ch9)

- **OUT pos** は検出器の退避位置（顕微鏡を入れることができる位置）、**IN pos** は X 線回折の測定位置です。
- **Spd** は、このアプリから制御する際に適用されるステージの移動速度です。

### Microscope (Ch6, 7, 8)

- Ch6 および Ch7 は、**Move** ボタンを押すと、入力した値（絶対値）へ移動します。Ch7 を頻繁に切り替える場合などに、キーボード入力で目的の位置まで移動させられるので便利です。
- Ch8 には、**OUT pos**（退避位置。FPD の IN が許可される位置）と **IN pos**（試料観察位置）があります。
- **Spd** は、このアプリから制御する際に適用されるステージの移動速度です。

### ショートカット

検出器 OUT → 顕微鏡 IN、および顕微鏡 OUT → 検出器 IN を、ひとつながりの動作として行うコマンドです。二つの動作が同時に実行されることはありません。最初の動作が終わり、ステージが完全に停止したことを確認してから、次の動作に移ります。

### 通常停止、緊急停止

PM16C コントローラーには、通常停止（減速停止）および緊急停止（即時停止）の二つのコマンドがあります。

## 表示の自動更新

この画面が前面にあるとき、アプリケーションは 0.5 秒ごとにステージの現在地を読み取り、入力欄および現在のステージ位置の表示を更新します。また、このアプリが前面に来たときにも、改めてステージの現在地を読み直して表示を更新します。したがって、手動でステージを動かした結果がこのアプリに反映されない、ということは起こりません。

## 安全対策

Ch8 と Ch9 の IN/OUT 操作は、ステージ同士を正面から衝突させるおそれのある操作です。衝突を防ぐため、以下の安全対策が実装されています。

1. **入力値チェック**：Ch9 の OUT 位置は −30000 より小さくなければなりません。また、Ch8 の OUT 位置は 0 より小さくなければなりません。
2. **移動時の再チェック**：Ch8 を 0 より大きい位置へ動かすコマンド、または Ch9 を −30000 より大きい位置へ動かすコマンドが送られたときは、Ch8 および Ch9 の現在地を読み取り、安全な範囲にあることを確認してから、実際の移動コマンドを送信します。

:::note
これらの安全対策のパラメータは、すべてのアプリに例外なく共通して適用されるため、各アプリ内ではなく [utils/stage/move_constraints.py](https://github.com/khsacc/bl18c_controller/blob/main/utils/stage/move_constraints.py) にまとめて定義されています。他のステージにもリミッターをかけたい場合は、`MOVE_CONSTRAINTS` に同じ形式の規則を追加します。Ch8 と Ch11（回転ステージ）の衝突を防ぐ規則も同ファイルに用意されていますが、実機での検証が済んでいないため、現在は無効（コメントアウト）になっています。
:::

```python
CH9_CH8_SAFE_BOUNDARY = -30000

MOVE_CONSTRAINTS = [
    # Ch9 > CH9_CH8_SAFE_BOUNDARY requires Ch8 <= 0
    # Moving Ch9 TO the boundary or more negative (OUT direction) is always safe.
    # Only moving Ch9 INTO the beam path is restricted.
    {
        'target_ch': 9, 'target_op': '>', 'target_val': CH9_CH8_SAFE_BOUNDARY,
        'required': [
            {'ch': 8, 'op': '<=', 'val': 0},
        ],
    },
    # Ch8 > 0 requires Ch9 <= CH9_CH8_SAFE_BOUNDARY
    # Moving Ch8 TO 0 or more negative (OUT direction) is always safe.
    # Only moving Ch8 INTO the beam path is restricted.
    {
        'target_ch': 8, 'target_op': '>', 'target_val': 0,
        'required': [
            {'ch': 9, 'op': '<=', 'val': CH9_CH8_SAFE_BOUNDARY},
        ],
    },
]
```
