---
sidebar_position: 0
title: Slack通知
---

# Slack通知

[Experimental Scheduler](../apps/exp-scheduler/index.md) の実行状況を Slack で受け取ることができます。

- **通知**：シーケンスの完了、停止条件による終了、エラーによる停止のときに、指定したチャンネルへメッセージを投稿します。シーケンスの開始時や、ユーザーが **Stop** を押したときには投稿しません。
- **スラッシュコマンド**：ボットとのダイレクトメッセージで `/bl18c` コマンドを送ると、実行状況・進捗・ステップ・LakeShore・PACE5000・試料像・スクリーンショット・XRD 生画像などを確認できます。

通知を有効にするには、Experimental Scheduler のツールバーにある **Notify on Slack** にチェックを入れます。Slack の設定がされていない場合、このチェックボックスは選択できません。

トークンや通知先チャンネルの設定方法は [Slack notifications](../settings/slack.md) を参照してください。

:::info
Slack との通信に失敗しても、実行中のシーケンスには影響しません。また、`--debug` モードでは通知の送信もスラッシュコマンドへの応答も行いません。
:::
