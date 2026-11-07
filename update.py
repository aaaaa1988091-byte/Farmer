"""FarmBot 自動更新：從 GitHub 比對差異，只更新有變動的腳本。

在 Minescript 裡執行：
    \\update             比對並更新（有任何錯誤就整批不動）
    \\update check       只看有哪些差異，不改任何檔案
    \\update restart     更新後請 Farm.py 重啟（Farm_watchdog / Discord bot 變動需自行 F9 後重開）
    \\update rollback    還原最近一次更新前的備份

.env 設定（每個帳號自己的資料夾各設一份，更新不會動 .env）：
    FARMBOT_REPO=你的帳號/倉庫名          必填，例如 alice/FarmBot
    FARMBOT_BRANCH=main                  選填，預設 main
    FARMBOT_REPO_PATH=                   選填，腳本在倉庫子資料夾時填，例如 minescript
    FARMBOT_GITHUB_TOKEN=                選填，私有倉庫必填；公開倉庫填了可避開 API 次數限制

運作方式：
    1. 取得倉庫檔案清單（含每個檔案的 git blob SHA）
    2. 本機檔案算出同樣的 SHA 比對 → 找出新增 / 修改的檔案（不用先下載）
    3. 只下載有差異的檔案，驗證 SHA、並檢查 Python 語法
    4. 備份舊檔到 backup/<時間>/ → 逐檔原子替換（失敗自動還原）→ update.py 本身最後才換
"""
from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import shutil
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from system.lib.minescript import echo as _echo
except ImportError:        # 直接用 python 在終端機測試時
    _echo = print

ROOT = Path(__file__).resolve().parent
BACKUP_DIR = ROOT / "backup"
TMP_DIR = ROOT / ".update_tmp"
DIFF_FILE = ROOT / "update_last.diff"
LOG_FILE = ROOT / "update.log"

INCLUDE_SUFFIXES = (".py",)                       # 要同步的檔案類型
SKIP_DIRS = {"backup", "instances", "__pycache__", ".update_tmp", ".git", ".github"}
KEEP_BACKUPS = 10
MAX_FILE_BYTES = 2_000_000


def say(message: str) -> None:
    _echo(message)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


class UpdateError(Exception):
    pass


# ───────────── 設定 ─────────────
def read_env() -> dict:
    values: dict = {}
    try:
        for raw in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    for key in ("FARMBOT_REPO", "FARMBOT_BRANCH", "FARMBOT_REPO_PATH", "FARMBOT_GITHUB_TOKEN"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


# ───────────── GitHub ─────────────
def _request(url: str, token: str, accept: str) -> bytes:
    headers = {"User-Agent": "FarmBot-Updater", "Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("找不到倉庫或分支（私有倉庫請設定 FARMBOT_GITHUB_TOKEN）") from exc
        if exc.code in (401, 403):
            remaining = exc.headers.get("X-RateLimit-Remaining")
            if remaining == "0":
                raise UpdateError("GitHub API 次數用完了（未登入每小時 60 次），請設定 FARMBOT_GITHUB_TOKEN 或稍後再試") from exc
            raise UpdateError(f"GitHub 拒絕存取（HTTP {exc.code}），請確認 FARMBOT_GITHUB_TOKEN 權限") from exc
        raise UpdateError(f"GitHub 回應錯誤 HTTP {exc.code}") from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise UpdateError(f"連不上 GitHub：{exc}") from exc


def fetch_remote_files(repo: str, branch: str, subpath: str, token: str) -> dict:
    """回傳 {本機相對路徑: {"sha": blob sha, "size": bytes}}。"""
    url = f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"
    data = json.loads(_request(url, token, "application/vnd.github+json").decode("utf-8"))
    if data.get("truncated"):
        say("§e倉庫檔案太多，GitHub 清單被截斷，部分檔案可能沒檢查到")
    prefix = subpath.strip("/")
    prefix = prefix + "/" if prefix else ""
    files = {}
    for entry in data.get("tree", []):
        if entry.get("type") != "blob":
            continue
        path = entry["path"]
        if prefix:
            if not path.startswith(prefix):
                continue
            path = path[len(prefix):]
        parts = Path(path).parts
        if not parts or ".." in parts or Path(path).is_absolute() or any(p in SKIP_DIRS or p.startswith(".") for p in parts):
            continue
        if not path.lower().endswith(INCLUDE_SUFFIXES):
            continue
        files[path] = {"sha": entry["sha"], "size": int(entry.get("size", 0))}
    return files


def fetch_blob(repo: str, sha: str, token: str) -> bytes:
    # 用 blob API 而不是 raw.githubusercontent：raw 有 CDN 快取，可能拿到舊版
    url = f"https://api.github.com/repos/{repo}/git/blobs/{sha}"
    data = json.loads(_request(url, token, "application/vnd.github+json").decode("utf-8"))
    if data.get("encoding") != "base64":
        raise UpdateError(f"未知的 blob 編碼：{data.get('encoding')}")
    return base64.b64decode(data["content"])


def git_blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def local_matches(content: bytes, remote_sha: str) -> bool:
    # Windows 的 git 可能把 LF 轉成 CRLF，兩種換行都比一次，避免永遠被判成有差異
    if git_blob_sha(content) == remote_sha:
        return True
    return git_blob_sha(content.replace(b"\r\n", b"\n")) == remote_sha


# ───────────── 更新流程 ─────────────
def diff_stats(old: bytes, new: bytes) -> tuple[int, int, list[str]]:
    old_lines = old.decode("utf-8", "replace").replace("\r\n", "\n").splitlines()
    new_lines = new.decode("utf-8", "replace").replace("\r\n", "\n").splitlines()
    lines = list(difflib.unified_diff(old_lines, new_lines, "本機", "GitHub", lineterm="", n=2))
    added = sum(1 for l in lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in lines if l.startswith("-") and not l.startswith("---"))
    return added, removed, lines


def prune_backups() -> None:
    try:
        folders = sorted(p for p in BACKUP_DIR.iterdir() if p.is_dir())
    except OSError:
        return
    for old in folders[:-KEEP_BACKUPS]:
        shutil.rmtree(old, ignore_errors=True)


def run_update(check_only: bool, restart: bool) -> int:
    env = read_env()
    repo = env.get("FARMBOT_REPO", "").strip().strip("/")
    if not repo or "/" not in repo:
        raise UpdateError("尚未設定倉庫：請在 .env 加上 FARMBOT_REPO=你的帳號/倉庫名")
    branch = env.get("FARMBOT_BRANCH", "main").strip() or "main"
    subpath = env.get("FARMBOT_REPO_PATH", "")
    token = env.get("FARMBOT_GITHUB_TOKEN", "").strip()

    say(f"檢查更新：{repo}@{branch}")
    remote = fetch_remote_files(repo, branch, subpath, token)
    if not remote:
        raise UpdateError("倉庫裡沒有找到可同步的檔案（確認分支與 FARMBOT_REPO_PATH）")

    new_files, changed_files = [], []
    for rel, info in sorted(remote.items()):
        target = ROOT / rel
        if not target.is_file():
            new_files.append(rel)
        else:
            try:
                if not local_matches(target.read_bytes(), info["sha"]):
                    changed_files.append(rel)
            except OSError as exc:
                raise UpdateError(f"讀取本機檔案失敗 {rel}：{exc}") from exc

    todo = changed_files + new_files
    if not todo:
        say(f"§a已是最新（比對 {len(remote)} 個檔案）")
        return 0

    # 下載差異檔案 → 驗證 SHA → 檢查語法（任何一步失敗都不會動到本機）
    shutil.rmtree(TMP_DIR, ignore_errors=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    downloaded: dict = {}
    try:
        for rel in todo:
            if remote[rel]["size"] > MAX_FILE_BYTES:
                raise UpdateError(f"{rel} 檔案過大（{remote[rel]['size']} bytes），已中止")
            content = fetch_blob(repo, remote[rel]["sha"], token)
            if git_blob_sha(content) != remote[rel]["sha"]:
                raise UpdateError(f"{rel} 下載內容校驗失敗，已中止")
            if rel.lower().endswith(".py"):
                try:
                    compile(content, rel, "exec")
                except SyntaxError as exc:
                    raise UpdateError(f"{rel} 第 {exc.lineno} 行語法錯誤，GitHub 上的版本有問題，已中止：{exc.msg}") from exc
            staged = TMP_DIR / rel
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(content)
            downloaded[rel] = content

        # 差異摘要＋完整逐行差異寫進 update_last.diff
        diff_text = []
        say(f"發現 {len(changed_files)} 個修改、{len(new_files)} 個新增：")
        for rel in changed_files:
            added, removed, lines = diff_stats((ROOT / rel).read_bytes(), downloaded[rel])
            say(f"  ~ {rel}  (+{added} / -{removed} 行)")
            diff_text.append(f"===== {rel} =====\n" + "\n".join(lines) + "\n")
        for rel in new_files:
            say(f"  + {rel}  (新檔案，{len(downloaded[rel].splitlines())} 行)")
            diff_text.append(f"===== {rel}（新增）=====\n")
        try:
            DIFF_FILE.write_text("\n".join(diff_text), encoding="utf-8")
        except OSError:
            pass

        if check_only:
            say(f"§e僅檢查模式：沒有修改任何檔案（完整差異：{DIFF_FILE.name}）")
            return 0

        apply_update(downloaded, changed_files, new_files)
    finally:
        shutil.rmtree(TMP_DIR, ignore_errors=True)

    touched = set(todo)
    say(f"§a更新完成：{len(todo)} 個檔案（舊版備份在 backup/，完整差異：{DIFF_FILE.name}）")
    if "update.py" in touched:
        say("update.py 本身也已更新，下次執行就會使用新版")
    if restart:
        request_farm_restart()
    else:
        needs = sorted(touched - {"update.py"})
        if needs:
            say("§e新程式碼要重新啟動才會生效：F9 停止後重新執行 Farm，或用 \\update restart")
        if "Farm_watchdog.py" in touched:
            say("§eFarm_watchdog.py 有變動（含 Discord bot）：請 F9 後重新執行 Farm 才會載入新版")
    return 0


def apply_update(downloaded: dict, changed: list, added: list) -> None:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = BACKUP_DIR / stamp
    backup.mkdir(parents=True, exist_ok=True)
    for rel in changed:
        dest = backup / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
    (backup / "_manifest.json").write_text(
        json.dumps({"changed": changed, "added": added}, ensure_ascii=False), encoding="utf-8"
    )

    # update.py 自己最後才換
    order = [r for r in changed + added if r != "update.py"] + [r for r in changed + added if r == "update.py"]
    applied: list = []
    try:
        for rel in order:
            target = ROOT / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".new")
            temporary.write_bytes(downloaded[rel])
            os.replace(temporary, target)
            applied.append(rel)
    except OSError as exc:
        say(f"§c寫入 {rel} 失敗，還原已替換的檔案：{exc}")
        restore_backup(backup, quiet=True)
        raise UpdateError("更新失敗，已還原到更新前的狀態") from exc
    prune_backups()


def restore_backup(folder: Path, quiet: bool = False) -> int:
    try:
        manifest = json.loads((folder / "_manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise UpdateError(f"備份 {folder.name} 缺少 _manifest.json，無法還原")
    restored = 0
    for rel in manifest.get("changed", []):
        source = folder / rel
        if source.is_file():
            (ROOT / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, ROOT / rel)
            restored += 1
    for rel in manifest.get("added", []):
        try:
            (ROOT / rel).unlink()
            restored += 1
        except OSError:
            pass
    if not quiet:
        say(f"§a已還原 {restored} 個檔案（備份 {folder.name}）")
    return restored


def run_rollback() -> int:
    try:
        folders = sorted(p for p in BACKUP_DIR.iterdir() if p.is_dir() and (p / "_manifest.json").is_file())
    except OSError:
        folders = []
    if not folders:
        raise UpdateError("沒有可還原的備份")
    latest = folders[-1]
    restore_backup(latest)
    shutil.move(str(latest), str(latest.with_name(latest.name + "_rolledback")))   # 避免重複還原
    say("§e請 F9 後重新執行 Farm，讓還原的程式碼生效")
    return 0


def request_farm_restart() -> None:
    """透過 Farm.py 既有的本機控制 socket 要求重啟（跟 Discord 的重啟按鈕同一條路）。"""
    env = read_env()
    secret = env.get("FARMBOT_CONTROL_SECRET", "")
    try:
        port = int(env.get("FARMBOT_CONTROL_PORT", "38751") or "38751")
    except ValueError:
        port = 38751
    if not secret:
        say("§e.env 沒有 FARMBOT_CONTROL_SECRET，無法自動重啟 Farm.py")
        return
    payload = json.dumps({"action": "restart", "secret": secret}).encode("utf-8") + b"\n"
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=15) as conn:
            conn.settimeout(15)
            conn.sendall(payload)
            reply = conn.recv(4096).split(b"\n", 1)[0]
        result = json.loads(reply.decode("utf-8")) if reply else {}
        say(result.get("message") or result.get("error") or "已送出重啟要求")
    except (OSError, ValueError) as exc:
        say(f"§e Farm.py 沒有回應，無法自動重啟（可能沒在執行）：{exc}")


def main() -> int:
    args = [a.lower().lstrip("-") for a in sys.argv[1:]]
    try:
        if "rollback" in args:
            return run_rollback()
        return run_update(check_only="check" in args, restart="restart" in args)
    except UpdateError as exc:
        say(f"§c更新中止：{exc}")
        return 1
    except Exception as exc:                      # 不要讓例外把整個 job 炸掉卻沒訊息
        say(f"§c更新發生未預期錯誤：{type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
