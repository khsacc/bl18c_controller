---
sidebar_position: 3
title: Simple controller for all stages
---

# Simple controller for all stages

すべてのステージを、パルスモーターのパルス数に基づいて制御できるシンプルな制御系です。

![Simple controller for all stages のスクリーンショット](/img/apps/stage/simple_stg.png)

本アプリケーションでは、それぞれのステージについて、以下の操作を行えます。

1. 絶対パルス数を指定しての移動（**Move Abs**）
2. 現在値に対する相対値を指定しての移動（**Relative -**, **Relative +**）
3. プリセットされた 3 つの速度（L, M, H）から一つを選択する形での、ステージ移動速度の変更

:::warning[重要]
このアプリケーションは、ステージ操作に習熟したユーザー向けです。どのステージがどの動作に対応するかがわからないユーザーには、手動コントローラによる操作を推奨します。
:::
