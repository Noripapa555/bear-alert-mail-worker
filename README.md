# Bear Alert Mail Worker

自治体の配信メールから熊情報を取り出すクラウド受信処理です。
アプリ本体、受信メール本文、メールパスワードやGoogle認証情報は含まれていません。

## 現在の状態

受信・署名検証・熊情報抽出・Firestore保存・対象メール削除・FCM通知を実装。
本番処理は無効。2026-10-15にクラウドからのメール接続と、キー不要のGoogle認証・
Firestore読み書き・FCM検証に成功しました（実通知なし）。
最初の配信元について、再配信承諾と実メール形式の確認が必要です。
`MAIL_WORKER_ENABLED=true` を設定するまでジョブは実行しません。
北海道から順に配信元を追加します。現在は北海道警察「ほくとくん防犯メール」と
秋田県「クマダス」、岩手県「いわてモバイルメール」の候補設定があり、有効な再配信元設定はありません。福井県・福島県も受信登録済みの候補として追加していますが、再配信条件・実メール形式の確認は未了です。
岩手県は熊情報4配信の受信登録済みで、再配信条件は県の担当窓口へ照会中です。
道警の条件では再配信に事前承諾が必要です。秋田県は再配信の利用条件を窓口に確認中です。

## 動作

1. TLSで専用IMAP受信箱を読み、完全一致する配信元と実際のDKIM署名を検証。
2. 登録・認証メールや不明な形式は保持。確認済みの抽出規則から警報情報だけを生成。
3. Firestoreの非公開キューへ永続保存。受領確認後にそのUIDだけを完全削除。
4. 内容から生成した安定IDで重複を防止。24時間超の配信情報は保存のみ。
5. 公開警報一覧とFCMへ配信。送信結果不明時は自動再送せず確認待ち。

対象外メールは明示的な不要判定ルールに一致する場合だけ削除。
メール全体を公開せず、個別登録リンクやメールアドレスを含む本文は保持します。
日本語原文に6言語の見出し・原文注記を添えます。原文の自動翻訳はしません。
IMAP UIDPLUS非対応時は保存しても削除しません。

## 無料の実行構成

公開リポジトリの標準Ubuntu GitHub Actionsを約5分ごとに実行する構成です。
Macは不要。有料ランナー・有料API・Firebase Blaze/Cloud Functionsは使いません。
GitHubの実行遅延・実行漏れ、自治体の配信遅延、FCM未達があり、即時配信は保証されません。
60日間リポジトリに活動がない場合は、GitHubが定期実行を無効化する場合があります。

## 本番有効化前の設定

- Repository secret: `BEAR_MAIL_PASSWORD`（所有者が画面から入力）
- 接続先と専用アドレスも公開ログへ出ないようSecrets `BEAR_MAIL_HOST`, `BEAR_MAIL_ADDRESS` に保存。
- Repository variables: `BEAR_FIREBASE_PROJECT`
- `GCP_WORKLOAD_IDENTITY_PROVIDER`, `GCP_MAIL_SERVICE_ACCOUNT`
- Google Cloud Workload Identity Federationで、このリポジトリのmainブランチだけを信頼。
- 専用サービスアカウントにFirestore操作とFCM送信権限を付与。長期秘密鍵は作成しない。
- 実メールの送信元、DKIM署名ドメイン、本文抽出形式と再利用条件を確認して `mail_sources.json` を設定。
- スケジュール・未処理メール・送信確認待ち・保持メール容量を監視する運用を用意。
- 最後に `MAIL_WORKER_ENABLED=true` を設定。

`mailbox.json` は空の雛形です。環境変数で専用メールの接続情報を渡します。
Googleの認証情報は公式Auth ActionがOIDCから短期発行します。
ログには件数だけを出し、生メールや認証情報をログ・成果物・キャッシュへ保存しません。
書き込み権限を持つ人は受信処理を変更できるため、共同編集者を限定してください。

## テスト

```sh
python -m pip install -r requirements.txt
python -m unittest discover -p 'test_*.py'
```

本番メールボックスやFCMを使わず、署名改変、重複、保存失敗時の保持、
対象UIDだけの削除、通知結果不明時の停止を検証します。

参考：
- [北海道警察の公式案内](https://www.police.pref.hokkaido.lg.jp/info/seian/bouhan_mail/bouhan_mail.html)
- [GitHub Actionsの料金](https://docs.github.com/en/billing/concepts/product-billing/github-actions)
- [スケジュール実行の制約](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
