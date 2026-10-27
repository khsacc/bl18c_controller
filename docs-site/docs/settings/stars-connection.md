---
sidebar_position: 3
title: STARS connection
---

# STARS connection

STARS サーバーへの接続設定を行うページです。この接続は、[簡易シャッター制御](../apps/xrd/shutter-control.md)（X 線ストッパー）と **Tools → Read ring current** で共有されます。

| 項目 | 内容 |
| --- | --- |
| **IP Address** | STARS サーバーの IPv4 アドレス |
| **Port** | 6057（固定） |
| **Node name** | STARS のノード名 |
| **STARS keyword** | STARS のキーワード |

入力後、**Save connection settings** を押すと保存されます。

:::warning[重要]
設定を変更した後は、アプリケーションを再起動してください。再起動するまで新しい設定は反映されません。
:::

キーワードはこの制御 PC にのみ保存されます。なお、`--debug` モードではシミュレーションの応答が使われ、STARS サーバーには接続しません。
