# Security Policy

## 這個專案的等級

**PoC 等級的參考實作，不是產品。** 它被設計成跑在**受信任的內網**：

- FastAGI 預設只聽 `127.0.0.1:4573`。**不要**把它暴露到公網或不受信任的網段；
  協議層沒有認證、沒有速率限制、沒有抗 DoS 設計。
- 電話層（Asterisk）與 AGI 之間是明文 TCP；跨機器部署時請用私有網段或隧道。

## 敏感資產

| 資產 | 放哪裡 | 規則 |
|---|---|---|
| `ELEVENLABS_API_KEY`、`DBN_MYSQL_*` | 專案根目錄 `.env`（systemd `EnvironmentFile`） | 權限 `600`、**不入版控**、不進 rsync 樹、不寫進 shell 歷史 |
| `data/aliases.json`（小名，含真名） | sidecar | 權限 `600`、**不入版控**、`rsync` 加 `--exclude`；範本 `data/aliases.example.json` 才是公開的 |
| 逐人名音檔與 `manifest.json`（只在 `disclose_names=True` 的內線部署） | `/var/lib/asterisk/sounds/dbn/zh/` | 含姓名，與 wav 同權限、不對外 serve、不入版控 |
| 通話日誌（`out/calls/*.jsonl`）與錄音（`recordings/`） | 本機 | 含逐字稿與決策，屬個資；不入版控、評測完即清 |
| DB 連線 | `.env` 或 `/etc/freepbx.conf` | 用**唯讀**帳號（`SELECT ON asterisk.*`）；不要用 root |

`git archive` 只會帶到 tracked 檔；上面這些都在 `.gitignore`。若你要 fork 後自行部署，
請先確認 `git status` 與 `git ls-files` 沒有你的名冊與 key。

## 隱私設計（不是可選項）

- 預設**不念姓名**、不確認存在、不列舉數量、不念分機（理由見 `docs/design.md` §7.2）。
- 開啟 `disclose_names=True` 前必須：管理層書面同意、音檔與 manifest 權限與 wav 一致、
  不對外 serve。濫用時的後果是全公司名單可被逐姓氏列舉。

## 回報漏洞

請**不要**開公開 issue。用 GitHub 的 private vulnerability reporting
（Security → Report a vulnerability），或在 repo 的聯絡方式找到維護者。
請附：受影響版本／commit、重現步驟、影響評估（能否讀取名冊、能否繞過掛線政策、
能否讓 AGI 對外發請求）。我們會在確認後回覆處理時程。

## 已知的非目標

- 沒有認證與授權（來電者身分不參與決策）。
- 沒有速率限制、退避矩陣、metrics 系統。
- 401／403 的處理是 PoC 立場（直接掛線，不轉總機）；正式部署應改為落到人（`ROADMAP.md`）。
