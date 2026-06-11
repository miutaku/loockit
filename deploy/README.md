# 実機デプロイ（Raspberry Pi 4B / RKE2 agent）

SESAME4 / SESAME Bot1 を実機で試す手順です。BLE はホストの BlueZ を D-Bus 経由で使うため、
**まずベアメタルで疎通を確認 → そのあと k8s 化**するのが最短です。

## 0. 前提（Pi ホスト側）

```bash
# BlueZ が動いていること（RKE2 ホスト OS 上）
sudo apt install -y bluez
sudo systemctl enable --now bluetooth
rfkill unblock bluetooth
bluetoothctl show        # Powered: yes を確認
```

SESAME の鍵は公式アプリで QR を書き出し、<https://sesame-qr-reader.vercel.app/> でデコードして
`secret_key`（16進）/ `public_key` を取得します。

## 1. まずベアメタルで動作確認（推奨）

k8s の前に、Pi 上で直接動かして BLE が通ることを確認します。

```bash
git clone <this-repo> && cd loockit
python3 -m venv .venv && . .venv/bin/activate
pip install -e .          # arm64 wheels が入る（grpcio / cryptography など）

# 近くの SESAME を探して BLE アドレスを得る（デバイスの近くで実行）
loockit scan
#  BLE ADDRESS          MODEL        REGISTERED  RSSI
#  AA:BB:CC:DD:EE:FF    SESAME4      True        -52
#  11:22:33:44:55:66    SESAMEBOT1   True        -60

cp config.example.toml config.toml   # ble_address / 鍵を記入（または env で）
loockit run --config config.toml -v
```

別端末から:

```bash
grpcurl -plaintext <pi-ip>:50051 sesame.SesameService/ListDevices
# REST を有効化していれば:
curl -X POST http://<pi-ip>:8080/devices/front-door/lock
```

ここまで動けば BLE 制御は確定です。

## 2. arm64 イメージをビルドして RKE2 の containerd に取り込む

RKE2 は containerd を使う（docker は無い）ので、イメージを arm64 でビルドして取り込みます。

### 方法 A: Pi 上で nerdctl ビルド → そのまま k8s.io namespace へ

```bash
# nerdctl + buildkit があれば
sudo nerdctl --namespace k8s.io build -t loockit:0.1.0 .
```

### 方法 B: 別マシンで buildx → tar を Pi に転送 → ctr import

```bash
# ビルドマシン（arm64 クロスビルド）
docker buildx build --platform linux/arm64 \
  -t loockit:0.1.0 -o type=docker,dest=loockit.tar .
scp loockit.tar pi:/tmp/

# Pi 上で RKE2 の containerd に取り込む
sudo /var/lib/rancher/rke2/bin/ctr \
  -a /run/k3s/containerd/containerd.sock -n k8s.io \
  images import /tmp/loockit.tar
```

`imagePullPolicy: IfNotPresent` なので、取り込み済みならレジストリ不要です。

## 3. マニフェストを編集して apply

[k8s/loockit.yaml](k8s/loockit.yaml) を編集:

- `nodeSelector.kubernetes.io/hostname` → Bluetooth を持つ Pi のホスト名（`kubectl get nodes`）
- `Secret loockit-keys` → 各デバイスの鍵（`LOOCKIT_<ID大文字>_SECRET_KEY` など）
- `ConfigMap` の `ble_address` → `loockit scan` で得た値

```bash
kubectl apply -f deploy/k8s/loockit.yaml
kubectl -n loockit logs deploy/loockit -f
```

hostNetwork なので Pod IP = ノード IP です。`<pi-ip>:50051`（gRPC）/ `<pi-ip>:8080`（REST）で到達できます。

## なぜ privileged / hostNetwork / dbus が必要か

- **D-Bus マウント（/run/dbus）**: bleak はホストの BlueZ に D-Bus で話します。
- **privileged**: コンテナから BlueZ を操作する最も確実な方法（権限の細かい調整より堅実）。
- **hostNetwork**: BlueZ・mDNS はホストレベル。Matter/MQTT を使う場合も NAT を避けられます。
- **replicas: 1 / Recreate**: Bluetooth アダプタは 1 つ。複数 Pod が奪い合わないように。

## Matter / MQTT を有効にする場合

- **Matter**: ホストに `avahi-daemon` が必要（mDNS 広告）。`sudo apt install avahi-daemon avahi-utils`
  後、ConfigMap の `[matter] enabled = true` にして再 apply。`matter-device-state.json` は
  `/data`（hostPath `/var/lib/loockit`）に置けば再起動で鍵が保持されます（config の path を `/data/...` に）。
- **MQTT**: クラスタ内/外の MQTT ブローカ（Mosquitto 等）を指定。`[mqtt] enabled = true` + host/port。

## トラブルシュート

| 症状 | 対処 |
| --- | --- |
| `loockit scan` で何も出ない | デバイスに近づく / `rfkill unblock bluetooth` / `bluetoothctl power on` |
| `org.freedesktop.DBus...` エラー | ホストで `bluetooth.service` 起動・`/run/dbus` マウント確認 |
| Pod が BT を掴めない | privileged: true・hostNetwork: true・nodeSelector が対象 Pi か確認 |
| connect は通るが状態が来ない | デバイスから離れすぎ。RSSI を `loockit scan` で確認 |
