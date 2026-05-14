# Hash-Heroes Bot

Auto-bot untuk Telegram mini-app **Hash-Heroes**.  
Fitur utama: stake kartu **UR only**, claim mining rewards, buka pack — semua dikendalikan lewat Telegram bot dengan tampilan fleet per grup wallet.

---

## Fitur

| Fitur | Keterangan |
|---|---|
| Auto-stake UR | Hanya kartu rarity UR yang di-stake, skip semua rarity lain |
| Fleet management | Lihat semua grup wallet sekaligus, tombol Stake & Claim per grup |
| Import wallets | Import ribuan wallet dari `wallets/*/attempts.jsonl` ke `accounts.json` |
| Create wallets | Generate wallet TON baru, open starter pack, simpan otomatis |
| Claim rewards | Claim mining rewards semua akun / per grup |
| Reroll UR | Script terpisah untuk farming wallet sampai dapat kartu UR |

---

## Requirements

- Python 3.10+
- pip packages (lihat `requirements.txt`)
- Telegram Bot Token dari [@BotFather](https://t.me/BotFather)
- Telegram User ID kamu (numeric)

```bash
pip install -r requirements.txt
```

---

## Setup

### 1. Clone & install

```bash
git clone https://github.com/YOUR_USERNAME/hash-heroes-bot.git
cd hash-heroes-bot
pip install -r requirements.txt
```

### 2. Konfigurasi `.env`

Copy file template lalu isi:

```bash
cp .env.example .env
```

Edit `.env`:

```env
TELEGRAM_BOT_TOKEN=token_dari_botfather
TELEGRAM_ADMIN_ID=id_telegram_kamu
HEARTBEAT_HOURS=12
NOTIFY_PER_CLAIM=false
```

Cara dapat `TELEGRAM_ADMIN_ID`: kirim pesan ke [@userinfobot](https://t.me/userinfobot), bot akan balas dengan ID kamu.

### 3. Buat `accounts.json`

Copy dari contoh:

```bash
cp accounts.example.json accounts.json
```

Format isi:

```json
[
  {
    "name": "namaakun",
    "user_id": 12345
  },
  {
    "name": "akun2",
    "wallet_address": "UQBxxx..."
  }
]
```

Fields:
- **`name`** — label bebas, harus unik, dipakai di log dan totals
- **`user_id`** — ID numerik dari URL API (`/api/me/12345`). Cara dapat: buka mini-app, DevTools → Network, lihat request `me/...`
- **`wallet_address`** — alamat TON wallet. Pakai ini kalau tidak tahu `user_id`; bot akan login otomatis dan resolve ID-nya
- **`ref`** — referral code (opsional, hanya dipakai waktu signup pertama)

Cukup isi salah satu: `user_id` **atau** `wallet_address`.

---

## Menjalankan

### Bot worker (background, auto-loop)

```bash
# satu kali jalan untuk semua akun
python bot.py

# loop terus, tidur sampai claim berikutnya ready
python bot.py --loop

# hanya satu akun
python bot.py --account namaakun

# skip langkah tertentu
python bot.py --no-open
python bot.py --no-stake
python bot.py --no-claim
```

### Telegram control bot

```bash
python tgbot.py
```

Bot akan online dan siap terima command di Telegram.  
Hanya admin (`TELEGRAM_ADMIN_ID`) yang bisa pakai.

---

## Telegram Commands

| Command | Fungsi |
|---|---|
| `/fleet` | Lihat semua grup wallet + tombol ⚡ Stake UR & 💰 Claim (instan) |
| `/groupstatus [grup]` | Live data satu grup: kartu, staked, pending, HP |
| `/dashboard` | Detail per wallet: saldo + kartu + mining |
| `/claimall [name]` | Claim mining rewards semua / satu akun |
| `/openpacks [name]` | Buka semua pack yang tersedia |
| `/importwallets [slot...]` | Import wallet dari folder `wallets/` ke `accounts.json` |
| `/createwallets [nama] [jumlah] [ref]` | Buat wallet TON baru + open pack |
| `/unstakeall [name]` | Unstake semua kartu |
| `/cards [name]` | Status kartu per akun |
| `/totals` | Lifetime totals semua akun |
| `/accounts` | List semua akun |
| `/help` | Daftar command |

### Contoh pakai

```
/fleet
```
→ Muncul semua grup (akun1, akun2, dst.) dengan jumlah wallet dan lifetime claim.  
Tekan **⚡ Stake UR akun1** → bot stake semua kartu UR idle di seluruh wallet grup akun1.

```
/groupstatus akun1
```
→ Fetch live data (API call) untuk semua wallet dalam grup akun1: berapa UR, berapa staked, pending HASH, dll.

```
/createwallets akun1 10
```
→ Buat 10 wallet baru (`akun1_1` s/d `akun1_10`), open starter pack, simpan ke `accounts.json`.

```
/importwallets slot1 slot2
```
→ Baca `wallets/slot1/attempts.jsonl` dan `wallets/slot2/attempts.jsonl`, import semua wallet valid ke `accounts.json`.

---

## Reroll UR (farming kartu UR)

Script `reroll.py` generate wallet baru terus-menerus sampai dapat kartu **UR Chrono Dragon**:

```bash
# test satu attempt
python reroll.py --account namaslot --once

# loop default (max 200 attempt)
python reroll.py --account namaslot

# custom limit
python reroll.py --account namaslot --max 500
```

Setiap mnemonic disimpan ke `wallets/<slot>/attempts.jsonl` **sebelum** network call — aman dari crash.  
Kalau menang, winner tersimpan ke `wallets/<slot>/winner.json`.

Setelah reroll selesai, import hasilnya ke bot:

```
/importwallets namaslot
```

---

## Struktur File

```
hash-heroes-bot/
├── api.py              # semua API call ke Hash-Heroes
├── bot.py              # worker utama (open pack, stake, claim)
├── tgbot.py            # Telegram interactive bot
├── config.py           # baca .env
├── wallet_gen.py       # generate TON wallet (mnemonic + address)
├── reroll.py           # farming UR card
├── notifier.py         # kirim notif Telegram
├── requirements.txt
├── .env.example        # template konfigurasi
├── accounts.example.json
├── docker-compose.yml
├── Dockerfile
└── deploy/
    ├── hashheroes-bot.service
    ├── hashheroes-tgbot.service
    ├── vps-setup.sh
    └── update.sh
```

File yang **tidak di-commit** (ada di `.gitignore`):
- `.env` — token & ID sensitif
- `accounts.json` — user_id dan wallet address real
- `totals.json` — data claim history
- `wallets/` — mnemonic private key semua wallet

---

## Deploy ke VPS

### Option A — systemd (recommended)

```bash
# push ke VPS
scp -r . user@vps:/tmp/hashheroes-bot-src

# setup di VPS
ssh user@vps
sudo SRC_DIR=/tmp/hashheroes-bot-src bash /tmp/hashheroes-bot-src/deploy/vps-setup.sh

# edit config
sudo -u hashheroes nano /opt/hashheroes-bot/.env
sudo -u hashheroes nano /opt/hashheroes-bot/accounts.json
sudo systemctl restart hashheroes-bot
sudo systemctl restart hashheroes-tgbot
journalctl -u hashheroes-bot -f
```

### Option B — Docker

```bash
docker compose up -d --build
docker compose logs -f
```

### Option C — tmux (quick)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env
cp accounts.example.json accounts.json && nano accounts.json
tmux new -s bot
python bot.py --loop
# Ctrl-b d untuk detach
# tmux attach -t bot untuk kembali
```

---

## Catatan Keamanan

- `accounts.json` berisi `user_id` dan `wallet_address` yang bisa dipakai untuk akses akun — **jangan pernah commit ke repo public**
- `wallets/` berisi **mnemonic private key** — siapapun yang punya file ini bisa ambil aset TON kamu
- `.env` berisi Telegram bot token dan admin ID — **jangan share**
- Semua file sensitif sudah ada di `.gitignore`
