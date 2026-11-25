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
  - 一覧には公開チャンネルと Bot が招待されている非公開（Private）チャンネルが表示されます。アーカイブ済みのチャンネルは表示されません。
  - 一覧取得には、Slack アプリの **OAuth & Permissions → Bot Token Scopes** に公開用の `channels:read`、非公開用の `groups:read` が必要です。`chat:write.public` は公開チャンネルへの投稿用で、非公開チャンネルの一覧取得には使えません。
- **Notify when**：通知するタイミングを選びます。
  - **Sequence completed**：シーケンスが完了したとき
  - **Sequence ended by its stop condition**：停止条件（例：ルビー圧力の到達）によって終了したとき
  - **Sequence stopped by an error**：エラーで停止したとき
- **Group identical errors for**：同じエラーがこの時間内に繰り返された場合、件数をまとめて 1 通の通知として送ります（0 にするとまとめません）。

### Private チャンネルが一覧に出ない場合

1. Slack アプリの **OAuth & Permissions → Bot Token Scopes** に `groups:read` を追加します。
2. **Reinstall to Workspace** で追加した権限を反映します。
3. 対象の Private チャンネルに、そのアプリの Bot が招待されていることを確認します。
4. Bot User OAuth Token が変わった場合は、この設定ページで入力し直して **Save credentials** を押します。
5. **Check credentials** で権限不足がないことを確認し、**Refresh channel list** で一覧を更新します。
6. チャンネルを選んで **Save Slack settings** を押します。

## Slash commands

**Answer /bl18c slash commands** にチェックを入れると、ボットとのダイレクトメッセージで `/bl18c` スラッシュコマンドに応答します（状態・進捗・ステップ・LakeShore・PACE5000・試料像・スクリーンショット・XRD 生画像・ネットワークカメラ画像の確認など）。このワークスペースのメンバーであれば誰でも利用できます。

`/bl18c camera` でネットワークカメラの画像を送る場合は、Windows のユーザー環境変数 `BL18C_CAMERA2_USERNAME` と `BL18C_CAMERA2_PASSWORD` にカメラの認証情報を設定してアプリケーションを再起動します。詳しくは [ネットワークカメラの画像](../slack/index.md#ネットワークカメラの画像) を参照してください。

設定を変更したら、最後に **Save Slack settings** を押して保存してください。

### コマンドの受信ログ

**Settings → Logging → Slack Slash Commands** は起動時からオンになっています。この PC が受信したスラッシュコマンドの日時、送信者のユーザー ID・ユーザー名、ワークスペース ID、チャンネル／DM ID、コマンドと引数、応答・拒否・エラーの区分を、ログ保存先の `slack_commands/YYYY-MM-DD.csv` に記録します。

記録を止めるにはチェックを外します。次回起動時には再びオンになります。`--debug` モードではスラッシュコマンドを受信しないため、このログも記録されません。
