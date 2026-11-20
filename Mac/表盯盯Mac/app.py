#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
表盯盯 · 本地版（只通知你自己）

和"通知同事"的版本不同：
- 不需要群机器人、不需要任何应用凭证
- 用你本机已登录的 lark-cli 读表
- 发现变化后用 macOS 系统通知 / 飞书私信你自己 / 群 webhook（三选一）

命令：
  python3 watch.py setup        交互式选表、选监听列、选通知方式
  python3 watch.py init         建立基线（不发通知）
  python3 watch.py run          比对 + 通知
  python3 watch.py run --dry   只打印不通知
  python3 watch.py list         看已配置的表
"""
import argparse
import contextlib
import csv
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HOME = Path.home()
try:
    os.chdir(Path(sys.argv[0]).resolve().parent)
except Exception:
    pass
DIR = HOME / ".tablewatch"
CONFIG = DIR / "config.json"
STATE = DIR / "state.json"

NAME_SPLIT = re.compile(r"[、,，/／;；\s]+")


CLI_CACHE = ""


def cli_path():
    """找到 lark-cli 的绝对路径：后台任务（launchd）的 PATH 很干净，必须用绝对路径。"""
    global CLI_CACHE
    if CLI_CACHE:
        return CLI_CACHE
    cands = []
    envp = os.environ.get("LARK_CLI") or ""
    if envp:
        cands.append(envp)
    try:
        saved = (load_json(CONFIG, {}) or {}).get("lark_cli") or ""
        if saved:
            cands.append(saved)
    except Exception:
        pass
    w = shutil.which("lark-cli")
    if w:
        cands.append(w)
    home = Path.home()
    dirs = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin",
            str(home / ".npm-global/bin"), str(home / ".local/bin")]
    appdata = os.environ.get("APPDATA") or ""
    if appdata:
        dirs.append(str(Path(appdata) / "npm"))
    for d in dirs:
        for name in ("lark-cli", "lark-cli.cmd", "lark-cli.exe", "lark-cli.bat"):
            cands.append(str(Path(d) / name))
    for pat in ("nvm/versions/node/*/bin/lark-cli", ".volta/bin/lark-cli", ".npm-global/bin/lark-cli"):
        try:
            cands += [str(x) for x in Path(home).glob(pat)]
        except Exception:
            pass
    try:
        nb = subprocess.run(["npm", "bin", "-g"], capture_output=True, text=True, timeout=10).stdout.strip()
        if nb:
            cands.append(str(Path(nb) / "lark-cli"))
    except Exception:
        pass
    for c in cands:
        try:
            if c and Path(c).exists():
                CLI_CACHE = c
                return CLI_CACHE
        except Exception:
            continue
    CLI_CACHE = "lark-cli"
    return CLI_CACHE


def cli(args):
    exe = cli_path()
    if exe == "lark-cli" and not shutil.which("lark-cli"):
        raise RuntimeError("本机找不到 lark-cli。双击「表盯盯.command」会自动安装。")
    cmdline = [exe] + args
    if IS_WIN and exe.lower().endswith((".cmd", ".bat")):
        cmdline = [os.environ.get("COMSPEC", "cmd.exe"), "/c"] + cmdline
    p = subprocess.run(cmdline, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"lark-cli 调用失败：{(p.stderr or p.stdout).strip()[:300]}")
    try:
        out = json.loads(p.stdout)
    except Exception:
        raise RuntimeError(f"lark-cli 输出不是 JSON：{p.stdout[:200]}")
    if not out.get("ok", False):
        raise RuntimeError(f"lark-cli 返回失败：{json.dumps(out, ensure_ascii=False)[:300]}")
    d = out.get("data", {})
    if not d and isinstance(out, dict) and ("csv" in out or "sheets" in out or "columns" in out):
        d = out
    return d


def ensure_dir():
    DIR.mkdir(parents=True, exist_ok=True)


def load_json(path, default):
    if not Path(path).exists():
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    ensure_dir()
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def tmpfile():
    """lark-cli 只允许把文件写到当前工作目录内，所以在 cwd 下建临时目录。"""
    d = Path.cwd() / ".lark-cli-tmp"
    d.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(suffix=".json", dir=str(d))
    os.close(fd)
    return name


# ---------------- 读表 ----------------
def fetch_csv(token, sheet):
    """直接把 CSV 读进内存（+csv-get 的 csv 字段走 stdout，不需要中间文件）。"""
    d = cli(["sheets", "+csv-get", "--spreadsheet-token", token, "--sheet-name", sheet])
    if not isinstance(d, dict):
        d = {}
    if "csv" not in d and isinstance(d.get("data"), dict):
        d = d["data"]
    if d.get("complete") is False:
        raise RuntimeError("这张表太大没读全：" + str(d.get("warning_message") or "")[:120])
    text = d.get("csv") or ""
    rows_idx = d.get("row_indices") or []
    if not text:
        # 旧版本 CLI（如 1.0.100）只给 annotated_csv：每逻辑行带 "[row=N] " 前缀
        ann = d.get("annotated_csv") or ""
        if ann:
            rows_idx = [int(x) for x in re.findall(r"^\[row=(\d+)\] ", ann, flags=re.M)] or rows_idx
            text = re.sub(r"^\[row=\d+\] ", "", ann, flags=re.M)
    if not text:
        raise RuntimeError("CLI 没返回表格内容（既没有 csv 也没有 annotated_csv）")
    recs = list(csv.reader(io.StringIO(text)))
    return recs, rows_idx


def token_of(url):
    m = re.search(r"/sheets/([A-Za-z0-9]+)", url or "")
    return m.group(1) if m else ""


def list_sheets(token):
    data = cli(["sheets", "+workbook-info", "--spreadsheet-token", token])
    return [s for s in data.get("sheets", []) if not s.get("is_hidden")]


KEY_PAT = re.compile(r"^(id|编号|序号|no|#)$", re.I)

def auto_key(header):
    """自动挑一个“每行唯一、不会被改”的列当认行键，用户不用再选。"""
    cols = [c for c in header if c]
    for c in cols:
        if KEY_PAT.match(c.strip()):
            return c
    for c in cols:
        if re.search(r"id|编号|序号", c, re.I):
            return c
    return cols[0] if cols else ""


def read_table(rule):
    recs, row_idx = fetch_csv(rule["token"], rule["sheet"])
    if not recs:
        return [], {}
    hr = int(rule.get("header_row", 1))
    header = [str(c).strip() for c in recs[hr - 1]]
    while header and not header[-1]:
        header.pop()
    if not header:
        raise RuntimeError("表头为空，检查 header_row")
    idx = {n: i for i, n in enumerate(header)}
    key_col = rule.get("key_col") or auto_key(header)
    rule["key_col"] = key_col
    rows = {}
    for off, r in enumerate(recs[hr:], start=hr + 1):
        vals = {n: (str(r[i]).replace("\r", "").strip() if i < len(r) else "") for n, i in idx.items()}
        if not any(vals.values()):
            continue
        key = vals.get(rule["key_col"], "")
        if not key:
            seed = "|".join(vals.get(c, "") for c in rule["watch_cols"])
            key = "H:" + hashlib.md5(seed.encode("utf-8")).hexdigest()[:10]
        real_row = row_idx[off - 1] if (off - 1) < len(row_idx) else off
        rows[f"{rule['id']}:{key}"] = {"row": real_row, "vals": vals}
    return header, rows


# ---------------- 比对 ----------------
def diff(old, new, watch_cols):
    added, deleted, modified = [], [], []
    for k, rec in new.items():
        if k not in old:
            added.append(rec)
            continue
        ch = [(c, old[k]["vals"].get(c, ""), rec["vals"].get(c, "")) for c in watch_cols
              if old[k]["vals"].get(c, "") != rec["vals"].get(c, "")]
        if ch:
            modified.append((rec, ch))
    for k, rec in old.items():
        if k not in new:
            deleted.append(rec)
    return added, deleted, modified


SCOPE = "im:message.send_as_user"


def scope_hint(err):
    e = str(err)
    if "missing_scope" in e or "send_as_user" in e:
        return ("发私信还差一个权限（im:message.send_as_user）。"
                "点配置页「③ 补授权（发私信）」按钮，或终端里运行："
                'lark-cli auth login --scope "im:message.send_as_user"')
    return e


def my_name(cfg):
    n = (cfg.get("notify") or {}).get("me_name") or ""
    if n:
        return n
    try:
        n = cli(["contact", "+get-user", "--as", "user"])["user"].get("name", "")
    except Exception:
        n = ""
    cfg.setdefault("notify", {})["me_name"] = n
    return n


def is_mine(rule, vals, me):
    """rule 里指定了「我的列」时，只认这一列里出现我名字的行；没指定就全收。"""
    col = rule.get("person_col") or ""
    if not col or not me:
        return True
    v = (vals.get(col) or "").strip()
    if not v:
        return False
    return me in v or v in me


def clip(s, n=90):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_row(vals, header, ch=None, max_cols=25):
    """把一整行按列逐条排出来；变了的字段写成 旧值 → 新值 并打星标。"""
    chd = {c: (o, n) for c, o, n in (ch or [])}
    out = []
    cols = [c for c in (header or []) if c]
    for c in cols[:max_cols]:
        if c in chd:
            o, n = chd[c]
            out.append(f"{c}：{clip(o, 60) or '空'} → {clip(n, 60) or '空'}  ★")
        else:
            out.append(f"{c}：{clip(vals.get(c, ''), 60) or '空'}")
    if len(cols) > max_cols:
        out.append(f"…还有 {len(cols) - max_cols} 列没列出")
    return out


def render(rule, added, deleted, modified, header=None):
    total = len(added) + len(deleted) + len(modified)
    lines = [f"【{rule['name']}】{datetime.now().strftime('%m-%d %H:%M')} · {total} 处改动"]
    for rec, ch in modified:
        lines.append("")
        lines.append(f"▍第 {rec['row']} 行 改动（{len(ch)} 个字段变了）")
        lines += ["  " + x for x in _fmt_row(rec["vals"], header, ch)]
    for rec in added:
        lines.append("")
        lines.append(f"▍第 {rec['row']} 行 新增")
        lines += ["  " + x for x in _fmt_row(rec["vals"], header)]
    for rec in deleted:
        lines.append("")
        lines.append(f"▍第 {rec['row']} 行 删除（原内容如下）")
        lines += ["  " + x for x in _fmt_row(rec["vals"], header)]
    if rule.get("url"):
        lines.append("")
        lines.append(rule["url"])
    return "\n".join(lines)


def test_text(cfg):
    ex = ("【示例表】11-20 16:50 · 1 处改动\n\n"
          "▍第 42 行 改动（2 个字段变了）\n"
          "  ID：41\n  模块：道具促销弹窗\n  修改状态：待修复 → 已通过  ★\n  修复人员：空 → 刘敏智  ★\n\n"
          "（这是一条示例，之后按真实改动发给你）")
    return ex


# ---------------- 通知 ----------------
def my_open_id(cfg):
    n = (cfg.get("notify") or {}).get("open_id") or ""
    if n:
        return n
    try:
        n = cli(["contact", "+get-user", "--as", "user"])["user"]["open_id"]
    except Exception:
        n = ""
    cfg.setdefault("notify", {})["open_id"] = n
    return n


def notify(cfg, rule, text, dry=False):
    """只走「群机器人 webhook」，不需要任何授权。"""
    url = (cfg.get("notify") or {}).get("webhook_url") or ""
    if dry:
        print("\n----- 将要发到群里 -----\n" + text + "\n")
        return
    if not url:
        raise RuntimeError("还没填群机器人的 webhook 地址")
    if (cfg.get("notify") or {}).get("at_me", True):
        oid = my_open_id(cfg)
        if oid:
            text = f'<at user_id="{oid}">{my_name(cfg) or ""}</at> ' + text
    import urllib.request
    payload = {"msg_type": "text", "content": {"text": text}}
    req = urllib.request.Request(url,
                                 data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    resp = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "ignore")
    try:
        j = json.loads(resp)
    except Exception:
        return
    # 飞书自定义机器人成功是 code=0；开了签名校验却没带签名会报 19021/19022
    code = j.get("code", j.get("StatusCode", 0))
    if code not in (0, None):
        msg = j.get("msg") or j.get("StatusMessage") or ""
        if "sign" in str(msg).lower() or str(code) in ("19021", "19022"):
            raise RuntimeError("这个群机器人开了「签名校验」，去群里把它关掉（或换成不校验的），再试一次")
        raise RuntimeError(f"群机器人返回失败：{code} {msg}")


# ---------------- setup 交互 ----------------
def ask(prompt, default=""):
    v = input(f"{prompt}{'（默认 ' + default + '）' if default else ''}: ").strip()
    return v or default


def setup():
    ensure_dir()
    cfg = load_json(CONFIG, {"notify": {"mode": "webhook"}, "rules": []})
    print("== 表盯盯 · 选表 ==")

    if not cfg["rules"]:
        cfg["rules"] = []

    while True:
        url = ask("粘贴表格链接（回车结束添加）")
        if not url:
            break
        token = token_of(url)
        if not token:
            print("  链接里没识别到 token，请复制完整地址（…/sheets/xxxx）")
            continue
        sheets = list_sheets(token)
        print("  子表：")
        for i, s in enumerate(sheets, 1):
            print(f"   {i}. {s['sheet_name']}（{s['row_count']} 行）")
        pick = ask("  选第几个", "1")
        try:
            s = sheets[int(pick) - 1]
        except Exception:
            print("  编号不对，跳过")
            continue

        # 读表头
        tmp_rule = {"token": token, "sheet": s["sheet_name"], "header_row": 1,
                    "key_col": "", "watch_cols": ["a"], "id": "tmp"}
        recs, _row_idx = fetch_csv(token, s["sheet_name"])
        header = [str(c).strip() for c in recs[0]] if recs else []
        while header and not header[-1]:
            header.pop()
        print("  列：")
        for i, c in enumerate(header, 1):
            print(f"   {i}. {c}")
        key_i = ask("  用哪列认行（填编号）", "1")
        key_col = header[int(key_i) - 1]
        title_i = ask("  消息里显示哪列做标题（填编号）", str(min(2, len(header))))
        title_col = header[int(title_i) - 1]
        print("  要监听哪些列？逗号分隔编号，直接回车=全部")
        sel = ask("  监听列")
        if sel.strip():
            watch = [header[int(x) - 1] for x in re.split(r"[,， ]+", sel) if x.strip()]
        else:
            watch = [c for c in header if c]

        rid = re.sub(r"[^\w]", "", s["sheet_name"])[:12] or f"t{len(cfg['rules']) + 1}"
        rule = {"id": rid, "name": f"{s['sheet_name']}", "url": url, "token": token,
                "sheet": s["sheet_name"], "header_row": 1, "key_col": key_col,
                "title_col": title_col, "watch_cols": watch}
        cfg["rules"] = [r for r in cfg["rules"] if r["id"] != rid] + [rule]
        print(f"  已加入：{rule['name']}（认行={key_col}，监听 {len(watch)} 列）\n")

    print("\n== 通知方式 ==")
    print("1. 飞书私信发给自己（推荐，手机也能收到）")
    print("2. macOS 系统通知（需要系统通知权限）")
    print("3. 群机器人 webhook（发到某个群）")
    m = ask("选一个", "1")
    if m == "3":
        cfg["notify"] = {"mode": "webhook", "webhook_url": os.environ.get("TW_HOOK") or ask("webhook 地址")}
    else:
        cfg["notify"] = {"mode": "system"}

    save_json(CONFIG, cfg)
    print(f"\n配置已保存：{CONFIG}")
    if ask("现在就建立基线？（y/n）", "y").lower().startswith("y"):
        init()
    print("\n完成。之后每 5 分钟检查一次由 install_mac.sh 安装的后台任务负责。")


# ---------------- 运行 ----------------
def init():
    cfg = load_json(CONFIG, {"notify": {"mode": "webhook"}, "rules": []})
    st = load_json(STATE, {"tables": {}})
    for rule in cfg["rules"]:
        header, rows = read_table(rule)
        st.setdefault("tables", {})[rule["id"]] = {"updated_at": datetime.now().isoformat(timespec="seconds"), "rows": rows, "header": header}
        print(f"{rule['name']}: {len(rows)} 条，基线已建立")
    save_json(STATE, st)


def ensure_job(cfg=None):
    """配置齐了就自动注册后台检查，不用手动点「启用」。"""
    cfg = cfg or load_json(CONFIG, {"notify": {}, "rules": []})
    if not cfg.get("rules"):
        return ""
    if not ((cfg.get("notify") or {}).get("webhook_url") or "").strip():
        return "还没填群机器人 webhook，暂不启用后台检查"
    try:
        return install_job()
    except Exception as e:
        return "自动注册后台检查失败：" + str(e)[:150]


def auto_baseline(cfg=None):
    """给还没有基线的表补一份基线（静默，不发通知）。已有基线的表不动。"""
    cfg = cfg or load_json(CONFIG, {"notify": {}, "rules": []})
    st = load_json(STATE, {"tables": {}})
    made = []
    for r in cfg.get("rules", []):
        ent = (st.get("tables") or {}).get(r.get("id"))
        if ent and ent.get("rows"):
            continue
        try:
            header, rows = read_table(r)
        except Exception as e:
            print(f"[{r.get('name')}] 自动建基线失败：{e}", file=sys.stderr)
            continue
        st.setdefault("tables", {})[r["id"]] = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "rows": rows, "header": header}
        made.append(f"{r.get('name')} {len(rows)} 条")
    if made:
        save_json(STATE, st)
        print("已自动建立基线：" + "，".join(made))
    return made


def run(dry=False):
    cfg = load_json(CONFIG, {"notify": {"mode": "webhook"}, "rules": []})
    if not cfg["rules"]:
        print("还没有配置任何表，先运行：python3 watch.py setup")
        return 1
    st = load_json(STATE, {"tables": {}})
    for rule in cfg["rules"]:
        try:
            header, rows = read_table(rule)
        except Exception as e:
            print(f"[{rule['name']}] 读表失败：{e}", file=sys.stderr)
            continue
        ent = st.get("tables", {}).get(rule["id"], {})
        old = ent.get("rows", {})
        header = ent.get("header") or header
        if not old:
            if not dry:
                st.setdefault("tables", {})[rule["id"]] = {"updated_at": datetime.now().isoformat(timespec="seconds"), "rows": rows, "header": header}
            print(f"[{rule['name']}] 首次运行，只建立基线（{len(rows)} 条）")
            continue
        added, deleted, modified = diff(old, rows, rule["watch_cols"] or header)
        me = my_name(cfg)
        if rule.get("person_col"):
            added = [r for r in added if is_mine(rule, r["vals"], me)]
            modified = [x for x in modified if is_mine(rule, x[0]["vals"], me)]
            deleted = [r for r in deleted if is_mine(rule, r["vals"], me)]
        if added or deleted or modified:
            text = render(rule, added, deleted, modified, header)
            notify(cfg, rule, text, dry)
            print(f"[{rule['name']}] 新增 {len(added)} | 修改 {len(modified)} | 删除 {len(deleted)} | " + ("预览，未通知" if dry else "已通知"))
        else:
            print(f"[{rule['name']}] 无变化")
        if not dry:
            st.setdefault("tables", {})[rule["id"]] = {"updated_at": datetime.now().isoformat(timespec="seconds"), "rows": rows, "header": header}
    if not dry:
        save_json(STATE, st)
    return 0


# ---------------- 本地配置台 ----------------
PLIST = Path.home() / "Library" / "LaunchAgents" / "com.tablewatch.plist"
PORT = 8799
IS_WIN = sys.platform.startswith("win")
TASK_NAME = "TableWatch"
try:
    APP_VERSION = hashlib.md5(Path(__file__).read_bytes()).hexdigest()[:8]
except Exception:
    APP_VERSION = "dev"


def health():
    out = {"lark": False, "login": False, "me": "", "open_id": "", "v": APP_VERSION}
    try:
        subprocess.run([cli_path(), "--version"], capture_output=True, timeout=10)
        out["lark"] = True
    except Exception:
        return out
    try:
        me = cli(["contact", "+get-user", "--as", "user"])["user"]
        out["login"] = True
        out["me"] = me.get("localized_name") or me.get("name") or ""
        out["open_id"] = me.get("open_id", "")
    except Exception:
        pass
    return out


INTERVALS = [1, 2, 5, 10, 30, 60]


def interval_min(cfg=None):
    cfg = cfg or load_json(CONFIG, {"notify": {}, "rules": []})
    try:
        v = int(cfg.get("interval_min") or 5)
    except Exception:
        v = 5
    return v if v in INTERVALS else 5


def task_installed():
    """后台任务是否已注册：Mac 看 plist，Windows 看计划任务。"""
    if IS_WIN:
        r = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME], capture_output=True, text=True)
        return r.returncode == 0
    return PLIST.exists()


def install_job():
    ensure_dir()
    mins = interval_min()
    cli_exe = cli_path()
    cli_dir = str(Path(cli_exe).parent) if cli_exe and cli_exe != "lark-cli" else "/usr/local/bin"
    (DIR / "app.py").write_text(Path(__file__).read_text(encoding="utf-8"), encoding="utf-8")

    if IS_WIN:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pyw if pyw.exists() else Path(sys.executable))
        tr = f'"{exe}" "{DIR / "app.py"}" run'
        cmd = ["schtasks", "/create", "/tn", TASK_NAME, "/tr", tr,
               "/sc", "minute", "/mo", str(mins), "/f"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip()[:300] or "schtasks 注册失败")
        subprocess.run(["schtasks", "/run", "/tn", TASK_NAME], capture_output=True)
        return f"已注册 Windows 计划任务：每 {mins} 分钟检查一次"

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.tablewatch</string>
  <key>ProgramArguments</key>
  <array><string>{sys.executable}</string><string>{DIR / "app.py"}</string><string>run</string></array>
  <key>StartInterval</key><integer>{mins * 60}</integer>
  <key>RunAtLoad</key><true/>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>{cli_dir}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>LARK_CLI</key><string>{cli_exe}</string>
  </dict>
  <key>StandardOutPath</key><string>{DIR / "run.log"}</string>
  <key>StandardErrorPath</key><string>{DIR / "run.log"}</string>
</dict></plist>
"""
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(plist, encoding="utf-8")
    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
    r = subprocess.run(["launchctl", "load", str(PLIST)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300] or "launchctl load 失败")
    return f"已注册：每 {mins} 分钟自动检查"


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200, ctype="application/json; charset=utf-8"):
        body = obj.encode("utf-8") if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            return self._send(HTML, ctype="text/html; charset=utf-8")
        if self.path == "/api/config":
            return self._send(load_json(CONFIG, {"notify": {"mode": "webhook"}, "rules": []}))
        if self.path == "/api/health":
            return self._send(health())
        return self._send({"error": "not found"}, 404)

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send({"ok": False, "error": "请求体不是合法 JSON"}, 400)
        try:
            if self.path == "/api/sheets":
                token = token_of(body.get("url", ""))
                if not token:
                    return self._send({"ok": False, "error": "没识别到表格 token，请粘贴完整链接"}, 400)
                sheets = list_sheets(token)
                m = re.search(r"[?&]sheet=([A-Za-z0-9]+)", body.get("url", "") or "")
                default = ""
                if m:
                    for it in sheets:
                        if it.get("sheet_id") == m.group(1):
                            default = it.get("sheet_name", "")
                            break
                return self._send({"ok": True, "token": token,
                                   "sheets": [{"name": it.get("sheet_name", ""), "id": it.get("sheet_id", "")} for it in sheets],
                                   "default": default or (sheets[0].get("sheet_name", "") if sheets else "")})

            if self.path == "/api/header":
                token = token_of(body.get("url", ""))
                recs, row_idx = fetch_csv(token, body["sheet"])
                header = [str(c).strip() for c in recs[0]] if recs else []
                while header and not header[-1]:
                    header.pop()
                n = sum(1 for r in recs[1:] if any(str(x).strip() for x in r))
                return self._send({"ok": True, "columns": header, "rows": n})

            if self.path == "/api/diag":
                token = token_of(body.get("url", ""))
                sh = body.get("sheet") or ""
                lines = []
                def cli_call(args):
                    p = subprocess.run(["lark-cli"] + args, capture_output=True, text=True)
                    lines.append("$ lark-cli " + " ".join(args))
                    lines.append("  rc=" + str(p.returncode))
                    if p.stdout.strip():
                        lines.append("  stdout: " + p.stdout.strip()[:400].replace("\n", " ⏎ "))
                        for k in ("csv", "annotated_csv", "complete"):
                            lines.append("  含 %s: %s" % (k, ("有" if ('"%s"' % k) in p.stdout else "无")))
                    if p.stderr.strip():
                        lines.append("  stderr: " + p.stderr.strip()[:400].replace("\n", " ⏎ "))
                cli_call(["--version"])
                if token:
                    cli_call(["sheets", "+workbook-info", "--spreadsheet-token", token])
                    if sh:
                        cli_call(["sheets", "+csv-get", "--spreadsheet-token", token, "--sheet-name", sh])
                return self._send({"ok": True, "text": "\n".join(lines)})

            if self.path == "/api/save":
                cfg = body.get("config") or {}
                if not cfg.get("rules"):
                    return self._send({"ok": False, "error": "至少配置一张表"}, 400)
                for r in cfg["rules"]:
                    if not r.get("token") or not r.get("sheet"):
                        return self._send({"ok": False, "error": f"「{r.get('name')}」缺表格或子表"}, 400)
                    # 认行键由程序自动推断，无需用户选
                cfg["lark_cli"] = cli_path()
                save_json(CONFIG, cfg)
                auto_baseline(cfg)
                note = ensure_job(cfg)
                return self._send({"ok": True, "msg": "已保存" + ("；" + note if note else "；后台检查已自动运行")})

            if self.path == "/api/test":
                incoming = (body.get("config") or {}).get("notify") or {}
                cfg = load_json(CONFIG, {"notify": {"mode": "webhook"}, "rules": []})
                if incoming:
                    cfg["notify"] = incoming
                    save_json(CONFIG, cfg)
                try:
                    notify(cfg, {"name": "表盯盯"}, test_text(cfg), False)
                except Exception as e:
                    return self._send({"ok": False, "error": scope_hint(e)})
                return self._send({"ok": True, "msg": "测试通知已发送，去飞书看看"})

            if self.path == "/api/init":
                cfg = load_json(CONFIG, {"notify": {"mode": "webhook"}, "rules": []})
                st = load_json(STATE, {"tables": {}})
                detail = []
                for r in cfg.get("rules", []):
                    header, rows = read_table(r)
                    st.setdefault("tables", {})[r["id"]] = {"updated_at": datetime.now().isoformat(timespec="seconds"), "rows": rows, "header": header}
                    detail.append({"name": r["name"], "rows": len(rows)})
                save_json(STATE, st)
                return self._send({"ok": True, "detail": detail})

            if self.path == "/api/install":
                cfg = load_json(CONFIG, {"notify": {}, "rules": []})
                auto_baseline(cfg)
                return self._send({"ok": True, "msg": install_job()})

            if self.path == "/api/runnow":
                buf = io.StringIO()
                try:
                    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                        run(False)
                    return self._send({"ok": True, "text": buf.getvalue()[-1500:] or "跑完了，没有输出"})
                except Exception as e:
                    msg = str(e)
                    if "urlopen" in msg or "Connection refused" in msg or "timed out" in msg:
                        msg = "发不到群：webhook 地址打不开（检查地址是否完整、签名校验是否关着）"
                    return self._send({"ok": False, "error": msg[:300], "text": buf.getvalue()[-800:]})

            if self.path == "/api/status":
                lg = DIR / "run.log"
                text = ""
                if lg.exists():
                    text = "\n".join(lg.read_text(encoding="utf-8", errors="ignore").splitlines()[-25:])
                st = load_json(STATE, {"tables": {}})
                last = ""
                for v in (st.get("tables") or {}).values():
                    t = v.get("updated_at") or ""
                    if t > last:
                        last = t
                return self._send({"ok": True, "installed": task_installed(),
                                   "interval": interval_min(), "last_check": last.replace("T", " "),
                                   "log": text or "（还没有检查记录）"})

            if self.path == "/api/quit":
                self._send({"ok": True, "msg": "配置页已关闭，后台检查继续"})
                threading.Timer(0.5, lambda: _SRV and _SRV.shutdown()).start()
                return

            if self.path == "/api/interval":
                cfg = load_json(CONFIG, {"notify": {}, "rules": []})
                try:
                    v = int(body.get("min") or 5)
                except Exception:
                    v = 5
                if v not in INTERVALS:
                    return self._send({"ok": False, "error": "间隔只能是 1/2/5/10/30/60 分钟"}, 400)
                cfg["interval_min"] = v
                save_json(CONFIG, cfg)
                if task_installed():
                    try:
                        return self._send({"ok": True, "msg": install_job()})
                    except Exception as e:
                        return self._send({"ok": False, "error": str(e)[:200]})
                return self._send({"ok": True, "msg": f"已设为每 {v} 分钟（启用后台检查后生效）"})

            if self.path == "/api/uninstall":
                if IS_WIN:
                    subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"], capture_output=True)
                elif PLIST.exists():
                    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
                    PLIST.unlink()
                return self._send({"ok": True, "msg": "已停止后台检查"})

            return self._send({"ok": False, "error": "unknown api"}, 404)
        except Exception as e:
            return self._send({"ok": False, "error": str(e)[:400]}, 500)


HTML = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>表盯盯</title>
<style>
*{box-sizing:border-box}
body{margin:0;padding:24px;font:14px/1.65 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#1d2129;background:#f7f8fa}
.wrap{max-width:920px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#86909c;font-size:13px;margin-bottom:18px}
.card{background:#fff;border:1px solid #e5e6eb;border-radius:10px;padding:18px;margin-bottom:14px}
label{display:block;font-size:13px;color:#6b7280;margin:10px 0 4px}
input,select{width:100%;padding:8px 10px;border:1px solid #e5e6eb;border-radius:8px;font-size:14px}
button{padding:8px 14px;border:1px solid #e5e6eb;border-radius:8px;background:#fff;font-size:13px;cursor:pointer}
button.p{background:#1664ff;border-color:#1664ff;color:#fff}
.chip{display:inline-block;border:1px solid #e5e6eb;border-radius:20px;padding:4px 12px;margin:6px 6px 0 0;font-size:13px;cursor:pointer}
.chip.on{border-color:#1664ff;background:#edf3ff;color:#1664ff}
.row{display:flex;gap:12px}.row>div{flex:1}
.acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.msg{font-size:13px;color:#0e9f6e;margin-top:10px}
.err{font-size:13px;color:#cb2634;margin-top:10px}
.muted{font-size:12px;color:#86909c;margin-top:6px}
</style></head><body><div class="wrap">
<h1>表盯盯</h1>
<div class="sub">选一张表，它每 5 分钟看一眼；有新增 / 修改 / 删除就发到你的飞书群。</div>
<div id="health" class="card"></div>
<div id="rules"></div>
<div class="acts"><button onclick="addRule()">+ 再加一张表</button></div>
<div class="card">
  <label>通知发到哪个群（群机器人 Webhook 地址）</label>
  <input id="hookUrl" placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxx" oninput="CFG.notify.webhook_url=this.value.trim()">
  <div class="muted">获取方式：目标群里点「⋯」→ 设置 → 群机器人 → 添加自定义机器人 → 复制 Webhook 地址。<b>签名校验请关掉</b>，否则发不出去。</div>
  <label style="margin-top:14px"><input type="checkbox" id="atme" style="width:auto;margin-right:8px" onchange="CFG.notify.at_me=this.checked">消息里 @ 我（手机会收到提醒）</label>
</div>
<div class="card">
  <label>检查间隔（有改动才发消息；没改动不发）</label>
  <select id="iv" style="max-width:200px" onchange="setIv(this.value)">
    <option value="1">每 1 分钟</option><option value="2">每 2 分钟</option>
    <option value="5">每 5 分钟</option><option value="10">每 10 分钟</option>
    <option value="30">每 30 分钟</option><option value="60">每 1 小时</option>
  </select>
  <div class="muted">第一次「保存」或「启用」时会自动建立基线，不用再手动点。</div>
  <div class="acts">
    <button onclick="testNotify()">先发一条测试通知</button>
    <button class="p" onclick="saveAll()">保存</button>
  </div>
  <div id="status" class="msg"></div>
  <div class="muted" id="statusLine"></div>
</div>
</div>
<script>
let CFG={notify:{mode:"webhook"},rules:[]},ME="";
async function post(p,b){const r=await fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(b)});return r.json()}
function setMode(m){CFG.notify.mode="webhook"}
function esc(v){return String(v==null?"":v).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;")}
function pickKey(cols){const exact=/^(id|编号|序号|no|#)$/i;let k=cols.find(c=>exact.test(c.trim()));if(!k)k=cols.find(c=>/(id|编号|序号)/i.test(c));return k||cols[0]||""}
function pickTitle(cols,key){const pat=/(描述|标题|名称|问题|bug|备注|模块|功能)/i;let t=cols.find(c=>c!==key&&pat.test(c));return t||cols.filter(c=>c!==key)[0]||cols[0]||""}
function paint(){
  document.getElementById("hookUrl").value=CFG.notify.webhook_url||"";
  const box=document.getElementById("rules");box.innerHTML="";
  CFG.rules.forEach((r,i)=>{
    const cols=(r._cols||[]).filter(c=>c);
    const el=document.createElement("div");el.className="card";
    const sheetSel=(r._sheets&&r._sheets.length)?('<select style="max-width:240px" onchange="pickSheet('+i+',this.value)">'+r._sheets.map(function(n){return '<option '+(n===r.sheet?'selected':'')+'>'+esc(n)+'</option>'}).join("")+'</select>'):'';
    const colOpts=function(v){return cols.length?cols.map(function(c){return '<option '+(c===v?'selected':'')+'>'+esc(c)+'</option>'}).join(""):'<option value="">（还没读到表头）</option>'};
    const personOpts='<option value="">（不选：这张表任何一行被改都通知我）</option>'
      +cols.map(function(c){return '<option '+(c===r.person_col?'selected':'')+'>'+esc(c)+'</option>'}).join("");
    el.innerHTML='<label>表格链接</label><input value="'+esc(r.url||"")+'" placeholder="粘贴飞书表格链接，直接点「读取子表」即可" oninput="fld('+i+',\'url\',this.value)">'
      +'<div class="acts"><button onclick="loadSheets('+i+')">① 读取子表</button>'+sheetSel
      +((r._err)?'<button onclick="diag('+i+')">诊断</button>':'')+'</div>'
      +'<label>哪一列里写着我的名字？（可选，例如「修复人员」或「人员」）</label>'
      +'<select onchange="fld('+i+',\'person_col\',this.value)">'+personOpts+'</select>'
      +'<div class="muted" style="margin-top:4px">选了「修复人员」这类列 → 只有这一列里写了你名字的那些行，改了才私信你；别人的行一律安静。不选 → 任何行变了都通知。</div>'
      +'<div class="muted" style="margin-top:12px">'+ (cols.length?('已读取 <b>'+cols.length+'</b> 列 · <b>'+(r._rows||0)+'</b> 行数据 · 自动认行：'+esc(r.key_col||"-")+'｜通知里会打出整条记录'):'还没读到表头，点上面「① 读取子表」') +' · 子表：'+esc(r.sheet||"未选")
      +'<span onclick="delRule('+i+')" style="float:right;color:#cb2634;cursor:pointer">删除这张表</span></div>'
      +(r._err?'<div class="err" style="margin-top:8px">'+esc(r._err)+'</div>':'')
      +(r._diag?'<pre style="margin-top:8px;background:#f5f6f8;border:1px solid #e5e6eb;border-radius:8px;padding:10px;font-size:12px;white-space:pre-wrap;word-break:break-all">'+esc(r._diag)+'</pre>':'');
    box.appendChild(el);
  });
  if(!CFG.rules.length) box.innerHTML='<div class="card muted">还没有表，点下面「+ 再加一张表」。</div>';
}
function fld(i,k,v){CFG.rules[i][k]=v}
function tog(i,j){const r=CFG.rules[i];const c=(r._cols||[]).filter(x=>x)[j];if(!c)return;r.watch_cols=r.watch_cols||[];const p=r.watch_cols.indexOf(c);p>=0?r.watch_cols.splice(p,1):r.watch_cols.push(c);paint()}
function addRule(){CFG.rules.push({id:"t"+Date.now().toString(36)+Math.floor(Math.random()*1000),name:"新表",url:"",sheet:"",token:"",key_col:"",person_col:"",watch_cols:[],_cols:[],_sheets:[],_err:""});paint()}
function delRule(i){CFG.rules.splice(i,1);paint()}
async function loadSheets(i){
  const r=CFG.rules[i];
  try{
    const d=await post("/api/sheets",{url:r.url});
    if(!d.ok){r._err=d.error||"读取子表失败";paint();return}
    r.token=d.token;r._sheets=(d.sheets||[]).map(function(s){return s.name});
    r.sheet=d.default||r._sheets[0]||"";r.name=r.sheet;r._err="";
    await loadHeader(i);
  }catch(e){r._err="读取子表出错："+(e&&e.message||e);paint()}
}
async function diag(i){
  const r=CFG.rules[i];
  try{
    const d=await post("/api/diag",{url:r.url,sheet:r.sheet});
    r._diag=d.ok?(d.text||"（没有输出）"):("诊断请求失败："+(d.error||""));
  }catch(e){r._diag="诊断出错："+(e&&e.message||e)}
  paint();
}
async function pickSheet(i,name){CFG.rules[i].sheet=name;await loadHeader(i)}
async function loadHeader(i){
  const r=CFG.rules[i];
  if(!r.sheet){r._err="先在上面的下拉里选中子表";paint();return}
  try{
    const d=await post("/api/header",{url:r.url,sheet:r.sheet});
    if(!d.ok){r._err=d.error||"读表头失败";paint();return}
    r._cols=(d.columns||[]).filter(function(c){return c&&String(c).trim()});
    r._rows=d.rows||0;r._err="";
    if(!r.key_col||r._cols.indexOf(r.key_col)<0) r.key_col=pickKey(r._cols);
    if(r.person_col&&r._cols.indexOf(r.person_col)<0) r.person_col="";
    r.watch_cols=r._cols.slice();
    paint();
  }catch(e){r._err="读表头出错："+(e&&e.message||e);paint()}
}
function clean(){const c=JSON.parse(JSON.stringify({notify:CFG.notify,rules:CFG.rules,interval_min:CFG.interval_min||5}));c.rules.forEach(r=>{delete r._cols;delete r._sheets;delete r._rows});return c}
async function saveAll(){const d=await post("/api/save",{config:clean()});st(d.ok?(d.msg||"已保存"):d.error,!d.ok);showStatus();return d.ok}
async function testNotify(){const d=await post("/api/test",{config:clean()});st(d.ok?(d.msg||"已发送"):d.error,!d.ok)}
async function initAll(){const d=await post("/api/init",{});st(d.ok?"基线已建立："+(d.detail||[]).map(x=>x.name+" "+x.rows+" 条").join("，"):d.error,!d.ok)}
async function showStatus(){
  try{
    const d=await post("/api/status",{});
    if(!d.ok) return;
    const last=(d.log||"").trim().split("\n").filter(Boolean).pop()||"";
    const bad=/失败|Errno|错误/.test(last)?'<div class="err" style="margin-top:4px">最近一条：'+esc(last)+'</div>':'';
    const box=document.getElementById("statusLine");
    box.innerHTML='后台检查：<b>'+(d.installed?"已启用":"未启用")+'</b>（每 '+d.interval+' 分钟）｜上次检查：'+esc(d.last_check||"暂无")
      +'<span onclick="uninstallJob()" style="float:right;cursor:pointer;text-decoration:underline">停止后台检查</span>'+bad;
  }catch(e){}
}
async function setIv(v){
  CFG.interval_min=parseInt(v,10)||5;
  const d=await post("/api/interval",{min:CFG.interval_min});
  st(d.ok?(d.msg||"已设置"):d.error,!d.ok);
  await showStatus();
}
async function installJob(){await saveAll();const d=await post("/api/install",{});st(d.ok?d.msg:d.error,!d.ok)}
async function uninstallJob(){const d=await post("/api/uninstall",{});st(d.ok?d.msg:d.error,!d.ok)}
function st(t,err){const e=document.getElementById("status");e.className=err?"err":"msg";e.textContent=t||""}
(async()=>{
  const h=await (await fetch("/api/health")).json();
  const box=document.getElementById("health");
  if(!h.lark) box.innerHTML='<div class="err">没找到 lark-cli，先双击同目录的「安装飞书CLI.command」完成安装。</div>';
  else if(!h.login) box.innerHTML='<div class="err">lark-cli 还没登录，双击「安装飞书CLI.command」按提示用飞书扫码授权。</div>';
  else box.innerHTML='<div class="msg">已连接飞书：'+(h.me||"")+'（读表用你自己的权限）</div>';
  if(h.open_id) CFG.notify.open_id=h.open_id;
  const c=await (await fetch("/api/config")).json();
  CFG.notify=Object.assign({mode:"webhook"},c.notify||{});
  CFG.rules=(c.rules||[]);
  CFG.interval_min=c.interval_min||5;
  const iv=document.getElementById("iv"); if(iv) iv.value=String(CFG.interval_min);
  const at=document.getElementById("atme"); if(at) at.checked=(CFG.notify.at_me!==false);
  if(!CFG.rules.length) addRule(); else paint();
  paint();
  // 打开页面就自动读取：已有链接的表不用再点按钮
  for(let i=0;i<CFG.rules.length;i++){
    const r=CFG.rules[i];
    if(r.url && !(r._cols||[]).length){
      try{ await loadSheets(i); }catch(e){}
    }
  }
  showStatus();
})();
</script></body></html>
"""


def _probe():
    """看看 8799 上是不是已经有一个表盯盯在跑；返回它的版本号或空串。"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8")).get("v", "")
    except Exception:
        return ""


def _kill_port_holder():
    """只处理占着 8799 的那个进程，绝不会误杀自己。"""
    try:
        if IS_WIN:
            out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
            for line in out.splitlines():
                if f":{PORT}" in line and "LISTENING" in line.upper():
                    pid = line.split()[-1]
                    if pid.isdigit() and int(pid) != os.getpid():
                        subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True)
        else:
            out = subprocess.run(["lsof", "-ti", f"tcp:{PORT}"], capture_output=True, text=True).stdout
            for pid in [int(x) for x in out.split() if x.isdigit()]:
                if pid != os.getpid():
                    try:
                        os.kill(pid, 15)
                    except Exception:
                        pass
    except Exception:
        pass


def _open(url):
    try:
        webbrowser.open(url)
    except Exception:
        pass


_SRV = None


def serve():
    global _SRV
    url = f"http://127.0.0.1:{PORT}"
    v = _probe()
    if v:
        if v == APP_VERSION:
            print(f"表盯盯已经在运行（版本 {v}），直接打开配置页：{url}")
            _open(url)
            print("（关掉这个窗口不影响后台检查）")
            return
        print(f"检测到旧版本还在运行（{v}），正在切换成新版本…")
        _kill_port_holder()
        time.sleep(1.5)

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
        _SRV = srv
    except OSError:
        print(f"端口 {PORT} 还被别的程序占着。已尝试打开配置页：{url}")
        print("如果打不开，重开一次终端、再双击「表盯盯.command」即可。")
        _open(url)
        return
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{PORT}"
    print(f"表盯盯配置页：{url}   （关掉这个窗口不影响后台检查）")
    try:
        note = ensure_job()
        if note:
            print(note)
    except Exception as e:
        print("后台检查自检失败：", e)
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        if IS_WIN:
            ensure_dir()
            try:
                f = open(DIR / "run.log", "a", encoding="utf-8", buffering=1)
                sys.stdout = f
                sys.stderr = f
            except Exception:
                pass
        sys.exit(run("--dry" in sys.argv))
    elif len(sys.argv) > 1 and sys.argv[1] == "init":
        init()
    elif len(sys.argv) > 1 and sys.argv[1] == "setup":
        setup()
    else:
        serve()
