---
sidebar_position: 6
title: Slack notifications
---

# Slack notifications

[Experimental Scheduler](../apps/exp-scheduler/index.md) の実行状況を Slack に通知するための設定ページです。シーケンスの完了、停止条件による終了、エラーによる停止のときに、指定したチャンネルへメッセージを投稿します。シーケンスの開始時や、ユーザーが **Stop** を押したときには投稿しません。

:::info
Slack との通信に失敗しても、実行中のシーケンスには影響しません。また、`--debug` モードでは通知の送信もスラッシュコマンドへの応答も行いません。
:::

## App credentials

| 項目 | 内容 |
| --- | --- |
| **Bot User OAuth Token** | `xoxb-` で始まるトークン（Slack アプリの OAuth & Permissions から取得） |
| **App-Level Token** | `xapp-` で始まるトークン（Basic Information → App-Level Tokens から取得）。スラッシュコマンドにのみ必要です。 |

- **Check credentials** で、入力したトークン（Bot トークンのスコープを含む）を Slack に問い合わせて確認します。このボタンでは保存は行われません。
- **Save credentials** で保存し、すぐに適用します。
- 欄を空にした場合は、環境変数 `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN` が使われます。

## Notifications

- **Send notifications to Slack**：通知を有効にします。
- **Channel**：通知先のチャンネルを選びます。**Refresh channel list** で一覧を更新し、**Send test message** でテストメッセージを送れます。
- **Notify when**：通知するタイミングを選びます。
  - **Sequence completed**：シーケンスが完了したとき
  - **Sequence ended by its stop condition**：停止条件（例：ルビー圧力の到達）によって終了したとき
  - **Sequence stopped by an error**：エラーで停止したとき
- **Group identical errors for**：同じエラーがこの時間内に繰り返された場合、件数をまとめて 1 通の通知として送ります（0 にするとまとめません）。

## Slash commands

**Answer /bl18c slash commands** にチェックを入れると、ボットとのダイレクトメッセージで `/bl18c` スラッシュコマンドに応答します（状態・進捗・ステップ・LakeShore・PACE5000・試料像・スクリーンショット・XRD 生画像の確認など）。このワークスペースのメンバーであれば誰でも利用できます。

設定を変更したら、最後に **Save Slack settings** を押して保存してください。
