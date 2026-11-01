---
sidebar_position: 2
title: 試料追従の設定
---

# 試料追従の設定

## Interactive Camera の設定を引き継ぐ

Interactive Camera の **Sample Tracking (Gasket Detection)** タブで準備したガスケット追従の設定は、**Sample Positions** パネルの **Import from Interactive Camera** で引き継げます。**Set up sample positions…** の隣にあります。

引き継ぐもの：

- 参照写真、ガスケットの輪郭（楕円）、試料位置（ガスケット中心または指定した点）、参照写真を撮影したときの Ch3/Ch4/Ch5
- ルビー位置（ガスケット中心からのオフセット）
- **Gasket tolerance**、**Average frames**、**Allow uniform ellipse scaling** の値

押すと確認の後、現在の参照写真・輪郭・試料位置・ルビー位置が置き換わり、**Position tracking** が **Gasket outline (Beta)** になります。シーケンス実行中は使えません。Interactive Camera 側で参照写真がない、輪郭が別の写真に対して付けられている、試料位置が未設定、または保存値が正しくない場合は、理由を表示して何も変更しません。すべての値を確認してから一度に置き換えるため、一部だけが変わることはありません。

引き継いだ後の設定は Scheduler 側で別に保存されます。どちらかで設定を変えても、もう一方には反映されません。
