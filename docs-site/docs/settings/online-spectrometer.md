---
sidebar_position: 2
title: Online spectrometer
---

# Online spectrometer

オンライン分光器（FluoRaPressée）への接続設定を行うページです。この設定は [Ruby Finder](../apps/scan/ruby-finder.md) と [Experimental Scheduler](../apps/exp-scheduler/index.md) で共有されます。

| 項目 | 内容 |
| --- | --- |
| **IP Address** | FluoRaPressée を動かしている PC の IPv4 アドレス（既定値：`192.168.1.102`） |
| **Port** | 8765（固定） |
| **API key** | FluoRaPressée の API キー（必須）。**Show API key** にチェックを入れると表示されます。 |

入力後、**Save connection settings** を押すと保存されます。

:::info
API キーはこの制御 PC にのみ保存され、実験シーケンスのファイルやログには書き出されません。
:::
