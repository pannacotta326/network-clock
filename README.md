# ネットワーク時計 (NetworkClock)

Windows 11 向けのデスクトップ時計アプリです。インターネット接続時は NTP でネットワーク時刻を表示し、オフライン時は PC 本体の時刻を表示します。

## 機能

- オンライン: NTP（`pool.ntp.org`）による時刻表示
- オフライン: ローカル時刻表示
- 「最前面・全画面」トグル（常に最前面 + 全画面の ON/OFF）
- タスクトレイから設定・終了
- 背景画像の差し替え
- システムにインストール済みのフォントを選択

## 必要環境

- Windows 11（Windows 10 でも動作する想定）
- Python 3.10 以上（exe を自分でビルドする場合）

## exe のビルド

```bat
build_exe.bat
```

完了後、`dist\NetworkClock.exe` が生成されます。

## 開発環境での実行

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python clock_app.py
```

## ライセンス

MIT License
