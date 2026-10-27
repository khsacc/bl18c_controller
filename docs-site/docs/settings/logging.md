---
sidebar_position: 4
title: Logging
---

# Logging

各アプリの詳細ログ（Details log）の保存先と、保存するかどうかを設定するページです。

## Details log output directory

- **Browse…** でログの保存先フォルダを選びます。**Reset** を押すと既定のフォルダ（`bl18c_controller/__localdata/`）に戻ります。保存先は次回起動時にも引き継がれます。
- **Per-app save location** に、アプリごとの実際の保存先（`<保存先>/<アプリ名>/`）が表示されます。

## Log Saving

詳細ログを保存するアプリを、チェックボックスで個別に選びます（**Select all** / **Unselect all** で一括選択・解除）。対象は以下のアプリです。

- DAC Scan / DAC Scan (Rot.) / XRD Scan
- Autofocus
- General 2D Scan / General 1D Scan
- Ruby Finder
- Sequence Pre-Validator
- Slack Slash Commands

:::info
チェックボックスの選択はアプリケーションを再起動するとすべて解除されます。
:::

アプリケーションを `--details` オプション付きで起動した場合は、チェックボックスの代わりにその旨のメッセージが表示され、すべてのアプリのログが常に保存されます。
