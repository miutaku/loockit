# loockit

ローカルファースト（BLE 直接制御）な **SESAME4 / SESAME Bot1** コントローラ。

- **ローカル BLE 制御**（SesameOS2 / `pysesameos2`）— Wi-Fi モジュール非依存、ホストの Bluetooth から直接制御
- **gRPC API** — 施錠 / 解錠 / トグル / Bot プッシュ（click）/ 状態取得 / リアルタイム状態ストリーム / 履歴取得
- **REST + WebSocket API** — gRPC と同等の操作を HTTP で。WebSocket でリアルタイム状態配信（任意）
- **リアルタイム状態監視** — SesameOS2 の通知を常時受信し、手動操作・他アプリ操作も即反映（自動再接続つき）
- **Matter ブリッジ** — SESAME4 を Door Lock、SESAME Bot1 を On/Off としてスマートホームに公開（**CANDY HOUSE クラウド API を使わず、ローカル BLE 制御の上に構築**）
- **MQTT / Home Assistant 連携** — MQTT Discovery で HA に自動登録（lock / button / battery）（任意）
- **操作履歴 / 状態永続化** — 状態変化・コマンドを SQLite に記録し REST/gRPC で取得（任意）
- **シミュレーションモード** — 実機なしで全経路を起動・検証
- **クラウド Web API v4 フォールバック**（任意・既定 OFF）

設計の詳細は [PURPOSE.md](PURPOSE.md)（要件定義）を参照。

## アーキテクチャ

```text
gRPC / REST / WebSocket ─┐                 ┌─ BleController ─ pysesameos2 ─ BLE ─ SESAME
Matter bridge ───────────┼── DeviceManager ┼─ FakeController (--simulate)
MQTT / Home Assistant ───┤   (状態キャッシュ ├─ CloudController (任意フォールバック)
HistoryRecorder ─────────┘    pub/sub・     │
                              ルーティング)  └─ HistoryStore (SQLite, 任意)
```

正規化された `DeviceState`（[src/loockit/models.py](src/loockit/models.py)）を全レイヤで共有し、
SESAME4 / SESAME Bot1 の差異を吸収しています。各インターフェース／ブリッジは独立した任意機能で、
どれかが失敗・未導入でもコア（BLE + gRPC）は止まりません。

## インストール

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e .            # コア（BLE + gRPC + 監視）
pip install -e ".[rest]"    # REST + WebSocket（fastapi + uvicorn）
pip install -e ".[mqtt]"    # MQTT / Home Assistant 連携（aiomqtt）
pip install -e ".[matter]"  # Matter ブリッジ（CircuitMatter、下記）
pip install -e ".[cloud]"   # クラウド Web API v4 フォールバック（任意）
pip install -e ".[dev]"     # 開発（pytest, grpcio-tools, fastapi, aiomqtt, httpx）
```

Linux で実機 BLE を使う場合は BlueZ が必要です（`sudo apt install bluez`）。

## 鍵の取得

公式 SESAME アプリで各デバイスの QR コードを書き出し、
[sesame-qr-reader](https://sesame-qr-reader.vercel.app/) でデコードして
`secret_key`（16進）と `public_key` を取得します。

## 設定

[config.example.toml](config.example.toml) を `config.toml` にコピーして編集します。

```toml
[grpc]
host = "0.0.0.0"
port = 50051

[[devices]]
id = "front-door"
model = "SESAME4"                  # lock / unlock / toggle 対応
ble_address = "24:71:89:cc:09:05"  # Linux=BD_ADDR / macOS=CoreBluetooth UUID
secret_key = "..."                 # 省略して環境変数でも可
public_key = "..."

[[devices]]
id = "desk-bot"
model = "SESAMEBOT1"               # click 対応
ble_address = "24:71:89:aa:bb:cc"
```

鍵をファイルに置きたくない場合は環境変数で上書きできます（デバイス id が `front-door` の場合）:

```bash
export LOOCKIT_FRONT_DOOR_SECRET_KEY=...
export LOOCKIT_FRONT_DOOR_PUBLIC_KEY=...
```

## 起動

```bash
# 実機 BLE
loockit run --config config.toml -v

# 実機なし（シミュレーション）— gRPC/監視/フォールバックの全経路を検証
loockit run --simulate --config config.toml -v

# Matter ブリッジも起動（[matter] extra + Matter SDK が必要）
loockit run --config config.toml --enable-matter -v
```

## Raspberry Pi での実機テスト

Raspberry Pi 4B+（内蔵 BLE）での手順。OS は Raspberry Pi OS Bookworm（64bit, Python 3.11+）を想定。

```bash
# 1) BlueZ と Python をインストール
sudo apt update
sudo apt install -y bluetooth bluez python3-venv python3-pip
sudo systemctl enable --now bluetooth
# scan を sudo なしで使う場合はユーザを bluetooth グループに追加（再ログイン要）
sudo usermod -aG bluetooth "$USER"

# 2) loockit を取得・インストール
git clone <this-repo> loockit && cd loockit
python3 -m venv .venv && . .venv/bin/activate
pip install -e .                 # 必要なら .[rest,mqtt,matter] を追加

# 3) 近くの SESAME を探して BLE アドレスを確認（デバイスの近くで実行）
loockit scan -d 20
#  BLE ADDRESS          MODEL        REGISTERED  RSSI
#  24:71:89:CC:09:05    SESAME4      True        -52
#  24:71:89:AA:BB:CC    SESAMEBOT1   True        -60
```

`scan` が権限エラーになる場合は `sudo $(which loockit) scan` で実行するか、上記の
bluetooth グループ追加後に再ログインしてください。前提として各デバイスは**公式 SESAME アプリで
初期設定済み**である必要があります（pysesameos2 の制約）。

```bash
# 4) 鍵を取得（公式アプリの QR を https://sesame-qr-reader.vercel.app/ でデコード）
#    config.example.toml をコピーし、scan で得た ble_address と鍵を記入
cp config.example.toml config.toml && nano config.toml

# 5) 起動して別端末から操作
loockit run --config config.toml -v
grpcurl -plaintext -d '{"device_id":"front-door"}' localhost:50051 sesame.SesameService/Lock
```

うまく接続できない場合は、デバイスのすぐ近く（数 m 以内）で再試行し、`-vv` で詳細ログを確認してください。
まずは `--simulate` で全体が動くことを確認してから実機に移ると切り分けが楽です。

### RKE2 / Kubernetes で動かす

Pi が RKE2 の agent ノードなら、k8s ワークロードとしてもデプロイできます。BLE はホストの BlueZ を
使うため、Pod は **hostNetwork + privileged + D-Bus ソケットマウント**で対象 Pi に固定します。
arm64 イメージのビルド・containerd への取り込み・マニフェスト適用までの手順は
[deploy/README.md](deploy/README.md)、マニフェストは [deploy/k8s/loockit.yaml](deploy/k8s/loockit.yaml) を参照してください。

> まずは上のベアメタル手順で BLE 疎通を確認してから k8s 化するのが切り分けの近道です。

## gRPC API

定義: [src/loockit/api/proto/sesame.proto](src/loockit/api/proto/sesame.proto)

| RPC | 用途 | 対象 |
| --- | --- | --- |
| `ListDevices` | 全デバイスと最新状態 | 全 |
| `GetStatus` | 単一デバイスの状態 | 全 |
| `Lock` / `Unlock` / `Toggle` | 施錠 / 解錠 / トグル | SESAME4 |
| `Click` | ボタンプッシュ | SESAME Bot1 |
| `StreamStatus` | リアルタイム状態ストリーム（server-streaming） | 全 |

モデルと操作の不整合（例: Bot に `Lock`）は `FAILED_PRECONDITION`、未知デバイスは `NOT_FOUND`、
通信失敗は `UNAVAILABLE` を返します。

`grpcurl` 例:

```bash
grpcurl -plaintext localhost:50051 sesame.SesameService/ListDevices
grpcurl -plaintext -d '{"device_id":"front-door"}' localhost:50051 sesame.SesameService/Lock
grpcurl -plaintext -d '{"device_id":"desk-bot"}'   localhost:50051 sesame.SesameService/Click
grpcurl -plaintext -d '{"device_id":"front-door"}' localhost:50051 sesame.SesameService/StreamStatus
```

`GetHistory` は `[history]` 有効時のみ結果を返します（無効時は `FAILED_PRECONDITION`）。

`.proto` を変更したらスタブを再生成します（`[dev]` extra 必要）:

```bash
python scripts/gen_proto.py
```

## REST + WebSocket API

`[rest]` extra を入れ、`config.toml` の `[rest] enabled = true` で有効化します
（[src/loockit/api/rest.py](src/loockit/api/rest.py)）。

| メソッド / パス | 用途 |
| --- | --- |
| `GET /healthz` | ヘルスチェック |
| `GET /devices` | 全デバイスの状態 |
| `GET /devices/{id}` | 単一デバイスの状態 |
| `POST /devices/{id}/lock`｜`/unlock`｜`/toggle`｜`/click` | 操作（body: `{"history_tag": "..."}` 任意） |
| `GET /history?device_id=&kind=&limit=` | 操作履歴（`[history]` 必須） |
| `WS /ws?device_id=` | リアルタイム状態ストリーム |

モデル不整合は `409`、未知デバイス／アクションは `404`、通信失敗は `503` を返します。

```bash
curl localhost:8080/devices
curl -X POST localhost:8080/devices/front-door/lock
curl -X POST localhost:8080/devices/desk-bot/click
websocat ws://localhost:8080/ws?device_id=front-door
```

## MQTT / Home Assistant 連携

`[mqtt]` extra を入れ、`[mqtt] enabled = true` とブローカ設定で有効化します
（[src/loockit/bridge/mqtt.py](src/loockit/bridge/mqtt.py)）。MQTT Discovery により Home Assistant に
自動登録されます:

- SESAME4 → `lock` エンティティ + `battery` センサー
- SESAME Bot1 → `button` エンティティ（press = 1 クリック）+ `battery` センサー

トピック（`base_topic = "loockit"` の場合）:

| トピック | 方向 | 内容 |
| --- | --- | --- |
| `loockit/<id>/availability` | 配信 | `online` / `offline` |
| `loockit/<id>/state` | 配信 | `LOCKED` / `UNLOCKED` |
| `loockit/<id>/battery` | 配信 | バッテリ % |
| `loockit/<id>/set` | 受信 | `LOCK` / `UNLOCK`（SESAME4） |
| `loockit/<id>/press` | 受信 | `PRESS`（Bot1） |

ブローカ切断時は自動再接続します。本ブリッジも DeviceManager 経由のローカル BLE 制御のみを使います。

## 操作履歴 / 状態永続化

SesameOS2 は端末側に履歴を保持しないため、loockit が状態変化とコマンドを SQLite に記録します
（[src/loockit/history.py](src/loockit/history.py)）。`[history] enabled = true` で有効化。
`GET /history`（REST）または `GetHistory`（gRPC）で取得できます。DB（`loockit-history.sqlite3`）は
gitignore 済みです。

## Matter ブリッジ

純 Python の Matter デバイス実装 [CircuitMatter](https://github.com/adafruit/CircuitMatter) を採用しており、
**connectedhomeip の C++ SDK ビルド不要**で実際に Matter ファブリックへ参加できます。

- SESAME4 → **Door Lock** エンドポイント（device type 0x000A / Door Lock クラスタ 0x0101）。
  `LockDoor` / `UnlockDoor` をローカル BLE 制御へ、`LockState` 属性を実機状態と同期。
- SESAME Bot1 → **On/Off** エンドポイント（device type 0x0100）。`On` = 1 回プッシュ
  （Bot はモーメンタリのため `Off` は no-op）。

ブリッジは **DeviceManager 経由のローカル BLE 制御のみ** を使い、CANDY HOUSE クラウド API には一切アクセスしません。
対応付けロジックは [src/loockit/bridge/matter.py](src/loockit/bridge/matter.py)、CircuitMatter 連携（Door Lock
クラスタ定義・デバイス生成）は [src/loockit/bridge/matter_circuit.py](src/loockit/bridge/matter_circuit.py) にあり、
ネットワークなしでユニットテスト済みです。

### 起動とコミッショニング

```bash
pip install -e ".[matter]"          # CircuitMatter
sudo apt install avahi-daemon avahi-utils   # mDNS 広告に必要
loockit run --config config.toml --enable-matter -v
```

起動するとコミッショニング用の **QR コードデータ** と **手動ペアリングコード** が出力されます:

```text
Listening on UDP port 5541
QR code data: MT:...
Manual code: 1350-375-7358
```

このコードを Apple Home / Google Home / Home Assistant などのコントローラに入力してコミッショニングします。
状態は `matter-device-state.json`（鍵・ファブリック情報を含むので gitignore 済み）に永続化されます。

CircuitMatter はホビー用途向けで Matter 認証は受けていません。`avahi-daemon` が無い環境では
Matter のみ自動無効化され、コア（BLE + gRPC + 監視）はそのまま動作します。**実ファブリックへの参加は
実機・実コントローラでの検証を推奨します。**

## Docker

コンテナを実行するホストの Bluetooth から直接制御が可能です。BLE はホストの BlueZ を
D-Bus 経由で使うため、host network + system D-Bus 共有が必要です（[docker-compose.yml](docker-compose.yml)）。

```bash
# 実機 BLE（ホストの Bluetooth が必要）
docker compose up --build

# 実機なし（シミュレーション）
docker compose run --rm loockit run --simulate -v
```

## テスト

```bash
pip install -e ".[dev]"
pytest
```

`models` / `config` / `manager`（フォールバック・pub/sub 含む）/ `gRPC`（ユニ + ストリーム + 履歴）/
`REST + WebSocket` / `MQTT マッピング` / `履歴ストア` / `Matter`（CircuitMatter）を実機なしでカバーします。

## ライセンス / クレジット

[LICENSE](LICENSE) 参照。ローカル BLE 制御は
[`pysesameos2`](https://github.com/mochipon/pysesameos2)、
クラウドフォールバックは [`pysesame3`](https://github.com/mochipon/pysesame3) を利用しています。
