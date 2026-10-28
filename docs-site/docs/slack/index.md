---
sidebar_position: 0
title: Slack
---

# Slack

[Experimental Scheduler](../apps/exp-scheduler/index.md) の実行状況を Slack で受け取ることができます。

## 通知

シーケンスの完了、停止条件による終了、エラーによる停止のときに、指定したチャンネルへメッセージを投稿します。シーケンスの開始時や、ユーザーが **Stop** を押したときには投稿しません。通知を有効にするには、Experimental Scheduler のツールバーにある **Notify on Slack** にチェックを入れます。Slack の設定がされていない場合、このチェックボックスは選択できません。

## スラッシュコマンド一覧

Slack Bot（Auto-18C）とのダイレクトメッセージで、以下のコマンドを送信することで実験の状況を把握することができます。

| コマンド | 内容 |
| --- | --- |
| `/bl18c`、`/bl18c status` | シーケンスの実行状態と現在のステップを表示します。 |
| `/bl18c progress` | 完了したステップ数、現在のループ、最新の進捗メッセージを表示します。 |
| `/bl18c steps`、`/bl18c sequence` | 実行中または最後のシーケンスのステップ一覧を表示します。 |
| `/bl18c lakeshore` | LakeShore 335 の温度、設定温度、昇温速度、ヒーター範囲を表示します。 |
| `/bl18c pace5000` | PACE5000 の現在圧力、目標圧力、圧力変化速度を表示します。 |
| `/bl18c sample` | 試料の温度、ガス圧、直近のルビー圧力測定値を表示します。 |
| `/bl18c screenshot` | 表示中の Experimental Scheduler ウィンドウ全体の画像を DM に送ります。 |
| `/bl18c screenshot [steps\|microscope\|pt\|xrd\|ruby]` | 指定した表示中のパネルの画像を DM に送ります（ステップ表、顕微鏡、圧力・温度、XRD、ルビー圧力）。 |
| `/bl18c xrd rawimg` | 直近の XRD 生画像を TIFF 形式で DM に送ります。 |
| `/bl18c help` | コマンドの使い方を表示します。 |



トークンや通知先チャンネルの設定方法は [Slack notifications](../settings/slack.md) を参照してください。

:::info
Slack との通信に失敗しても、実行中のシーケンスには影響しません。また、`--debug` モードでは通知の送信もスラッシュコマンドへの応答も行いません。
:::
