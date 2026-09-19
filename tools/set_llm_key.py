#!/usr/bin/env python3
"""一键写入并**当场验证**大模型 API Key。

为什么需要它（都是本项目真实踩过的坑）：
    1. **粘贴断行**：从网页复制长 Key 时，终端/编辑器容易把它折成两行，
       于是 .env 里只剩前半截（本项目实际出现过「Key 只有 6 个字符」）。
    2. **前缀大小写**：百炼套餐 Key 官方规定小写 `sk-sp-`，但控制台/输入法
       可能给出大写 `Sk-sp-` —— **即使 Base URL 完全正确也会 401**。
    3. ★**套餐与 Base URL 不配套**（最容易踩、最难自查的一个）★：
       阿里云百炼的三类 Key 各自绑定**不同的** Base URL，混用一律 401：
           sk-      按量付费    https://dashscope.aliyuncs.com/compatible-mode/v1
           sk-sp-   Token Plan  https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
           sk-ws-   Coding Plan https://coding.dashscope.aliyuncs.com/v1
       用通用地址去调套餐 Key，报错长得像「Key 无效」，很容易误判成 Key 复制错。

本脚本做四件事：
    ① 归一化：去掉所有空白字符（\\n \\r \\t 空格）与首尾引号，并把前缀纠正为小写；
    ② 套餐配对：识别 sk- / sk-sp- / sk-ws- 前缀，自动改用该套餐专属 Base URL；
    ③ 体检：按格式与配对关系给出警告（不适用的情况不瞎报）；
    ④ 实测：真的向 base_url 发一次最小请求，用 HTTP 状态码说话，再决定要不要写入。

用法：
    python tools/set_llm_key.py --key "sk-xxxxxxxx"        # 写入 + 实测
    python tools/set_llm_key.py                            # 交互式粘贴（推荐）
    python tools/set_llm_key.py --key "sk-xxx" --check      # 只测不写
    python tools/set_llm_key.py --show                      # 只看当前配置（脱敏）
    python tools/set_llm_key.py --models                    # 列出该 Key 可用模型

退出码：0 = 验证通过并已写入；1 = 验证失败（未写入）；2 = 用法/环境错误。
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DOTENV = PROJECT_ROOT / ".env"

# 标准 API Key 的典型形态：sk- 开头 + 一段十六进制/字母数字主体
_STD_KEY_RE = re.compile(r"^sk-[A-Za-z0-9_\-]{20,}$")

# ---------------------------------------------------------------------
# ★ 关键知识：阿里云百炼「套餐类型」与 Base URL **必须配套使用**，混用一律 401。
#   这张表是本工具最有价值的部分 —— 它把官方文档里的对应关系固化成代码。
#   文档：https://help.aliyun.com/zh/model-studio/environment-variable-hk
# ---------------------------------------------------------------------
KEY_PLANS: dict[str, tuple[str, str]] = {
    # 前缀: (套餐名, 该套餐专属的 OpenAI 兼容 Base URL)
    "sk-sp-": ("Token Plan（套餐版）", "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"),
    "sk-ws-": ("Coding Plan（套餐版）", "https://coding.dashscope.aliyuncs.com/v1"),
    "sk-": ("按量付费（通用）", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
}


def infer_base_url(key: str) -> tuple[str, str] | None:
    """按 Key 前缀推断应有的 Base URL。返回 (套餐名, base_url)，认不出则 None。

    注意顺序：必须先匹配更长的前缀（sk-sp- 在 sk- 之前），否则会把套餐 Key
    误判成通用 Key —— 这正是本项目真实踩过的坑。
    """
    for prefix in ("sk-sp-", "sk-ws-", "sk-"):
        if key.startswith(prefix):
            return KEY_PLANS[prefix]
    return None


def normalize(raw: str) -> str:
    """去掉所有空白与包裹引号，并修正前缀大小写。

    ① 断行、空格、误带的引号都在这一步被修掉；
    ② 百炼的套餐 Key 官方规定是小写 `sk-sp-`，但控制台/输入法可能给出
       大写 `Sk-sp-`，此时**即使 Base URL 正确也会 401**，所以顺手纠正。
    """
    s = "".join(raw.split())          # 移除全部空白字符（含 \n \r \t 空格）
    s = s.strip().strip("'\"")         # 去掉误加的引号
    if len(s) >= 3 and s[0] in "Ss" and s[1] in "Kk" and s[2] == "-":
        s = "sk-" + s[3:]              # Sk- / SK- / sK- → sk-
    return s


def inspect(key: str, base_url: str = "") -> list[str]:
    """按格式与套餐配对关系给出「可信度」提示，返回警告列表（空 = 正常）。

    重要更正：百炼的套餐 Key（sk-sp- / sk-ws-）**天然含有英文句点**，
    这是合法格式，不能据此判定为「签名令牌」。早期版本误判过，已修正。
    """
    warns: list[str] = []
    if not key:
        return ["Key 为空"]

    plan = infer_base_url(key)
    if plan:
        plan_name, want_url = plan
        if base_url and base_url.rstrip("/") != want_url.rstrip("/"):
            warns.append(
                f"Base URL 与套餐不配套：这是 {plan_name} 的 Key，"
                f"应使用 {want_url}，当前填的是 {base_url}。"
                "官方明确说明两者混用会返回 401 鉴权失败。"
            )
    else:
        # 前缀不是已知套餐格式，才需要对「签名令牌」这类错字段保持警惕
        if "." in key:
            warns.append(
                "含有英文句点，且前缀不在已知套餐列表内 —— "
                "可能是网关签发的签名令牌，而非标准 API Key。"
            )
        elif not _STD_KEY_RE.match(key):
            warns.append("未匹配常见的 sk-xxx 形态，可能是自定义前缀。")

    if len(key) < 20:
        warns.append(f"长度仅 {len(key)} 字符，对 API Key 来说过短（常见为 35 或 122 字符）。")
    if len(key) > 300:
        warns.append(f"长度 {len(key)} 字符，过长，可能把多行内容粘在了一起。")
    return warns


def mask(key: str) -> str:
    if len(key) <= 12:
        return key[:3] + "*" * max(0, len(key) - 3)
    return f"{key[:7]}...{key[-4:]}（共 {len(key)} 字符）"


def probe(base_url: str, model: str, key: str) -> tuple[bool, str]:
    """真的发一次最小请求。返回 (是否成功, 人类可读说明)。"""
    try:
        import httpx
    except ImportError:
        return False, "未安装 httpx，无法联网验证（可加 --no-probe 跳过）"

    url = base_url.rstrip("/") + "/chat/completions"
    body = {"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 5}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    # trust_env=False：外部地址不该走本机代理；若你确实需要代理，请自行改这里
    try:
        with httpx.Client(timeout=25.0, trust_env=False) as c:
            t0 = time.time()
            r = c.post(url, headers=headers, json=body)
            dt = time.time() - t0
    except Exception as e:  # noqa: BLE001 - 网络异常一律给出可读提示
        return False, f"网络异常 {type(e).__name__}: {e}"

    if r.status_code == 200:
        return True, f"HTTP 200，模型可用（{dt:.2f}s）"
    detail = (r.text or "")[:220].replace("\n", " ")
    if r.status_code == 401:
        return False, f"HTTP 401 认证失败 —— Key 无效或不属于该平台。原始返回：{detail}"
    if r.status_code == 404:
        return False, f"HTTP 404 模型或路径不存在 —— 检查 model / base_url。原始返回：{detail}"
    if r.status_code == 429:
        return False, f"HTTP 429 限流或欠费。原始返回：{detail}"
    return False, f"HTTP {r.status_code}。原始返回：{detail}"


def _upsert(lines: list[str], name: str, value: str) -> tuple[list[str], bool]:
    """把 `name=value` 写进行列表：命中已存在（含被注释）的行就替换，否则追加。

    返回 (新的行列表, 是否命中了已有行)。
    """
    out: list[str] = []
    hit = False
    for ln in lines:
        stripped = ln.strip()
        if stripped.startswith(f"{name}=") or stripped.startswith(f"# {name}="):
            out.append(f"{name}={value}")
            hit = True
        else:
            out.append(ln)
    return out, hit


def write_dotenv(key: str, base_url: str | None = None) -> tuple[bool, str]:
    """把 Key（以及可选的 base_url）写进 .env，保留其它内容与注释。"""
    if not DOTENV.exists():
        body = [
            "# BlindSpatialAgent 本地密钥文件（已被 .gitignore 忽略）",
            f"BSA_LLM_API_KEY={key}",
        ]
        if base_url:
            body.append(f"BSA_LLM_BASE_URL={base_url}")
        DOTENV.write_text("\n".join(body) + "\n", encoding="utf-8")
        return True, "已创建 .env"

    lines = DOTENV.read_text(encoding="utf-8").splitlines()

    backup = DOTENV.with_suffix(".env.bak")
    try:
        backup.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass  # 备份失败不阻塞主流程，但要提示

    out, hit = _upsert(lines, "BSA_LLM_API_KEY", key)
    notes = ["BSA_LLM_API_KEY"]
    if not hit:
        out.append(f"BSA_LLM_API_KEY={key}")

    if base_url:
        out, hit2 = _upsert(out, "BSA_LLM_BASE_URL", base_url)
        if not hit2:
            out.append(f"BSA_LLM_BASE_URL={base_url}")
        notes.append("BSA_LLM_BASE_URL")

    DOTENV.write_text("\n".join(out) + "\n", encoding="utf-8")
    return True, f"已写入 .env（{' + '.join(notes)}，旧文件备份至 {backup.name}）"


def main() -> int:
    ap = argparse.ArgumentParser(description="写入并实测大模型 API Key")
    ap.add_argument("--key", help="直接给出 Key；不给则进入交互式粘贴")
    ap.add_argument("--base-url", help="覆盖 base_url（默认读 config/.env）")
    ap.add_argument("--model", help="覆盖 model（默认读 config/.env）")
    ap.add_argument("--check", action="store_true", help="只验证，不写入 .env")
    ap.add_argument("--show", action="store_true", help="只显示当前配置（脱敏）后退出")
    ap.add_argument("--models", action="store_true", help="列出该 Key 可用的模型后退出")
    ap.add_argument("--no-probe", action="store_true", help="跳过联网验证")
    args = ap.parse_args()

    from config.loader import load_config  # noqa: PLC0415 - 延迟导入，保证 --help 不依赖项目环境

    cfg = load_config()
    base_url = args.base_url or cfg["llm"].get("base_url") or ""
    model = args.model or cfg["llm"].get("model") or ""
    cur = cfg["llm"].get("api_key") or ""

    if args.models:
        key = normalize(args.key or cur)
        if not key:
            print("没有可用的 Key（.env 未配置，也未用 --key 指定）。")
            return 2
        plan = infer_base_url(key)
        if plan:
            print(f"套餐识别 : {plan[0]}")
            base_url = plan[1]
        try:
            import httpx  # noqa: PLC0415
        except ImportError:
            print("未安装 httpx，无法查询。")
            return 2
        url = base_url.rstrip("/") + "/models"
        try:
            with httpx.Client(timeout=25.0, trust_env=False) as c:
                r = c.get(url, headers={"Authorization": f"Bearer {key}"})
        except Exception as e:  # noqa: BLE001
            print(f"网络异常 {type(e).__name__}: {e}")
            return 1
        if r.status_code != 200:
            print(f"HTTP {r.status_code}：{(r.text or '')[:200]}")
            return 1
        ids = [m.get("id") for m in (r.json().get("data") or [])]
        print(f"{base_url} 下可用模型 {len(ids)} 个：")
        for i in ids:
            print(f"  {i}")
        return 0

    if args.show:
        print("当前配置")
        print(f"  base_url : {base_url}")
        print(f"  model    : {model}")
        print(f"  api_key  : {mask(cur) if cur else '（未配置）'}")
        return 0

    raw = args.key
    if raw is None:
        print("请粘贴 API Key 后回车（输入不回显；直接回车取消）：")
        try:
            raw = getpass.getpass("  key> ")
        except (EOFError, KeyboardInterrupt):
            print("\n已取消。")
            return 2
    if not raw.strip():
        print("未提供 Key，已取消。")
        return 2

    key = normalize(raw)
    print("=" * 68)
    print(f"归一化后 : {mask(key)}")
    if key != raw:
        print("          （已自动去掉空白字符/引号，并纠正前缀大小写）")

    # ★ 按前缀推断该套餐应有的 Base URL，并据此自动配对
    plan = infer_base_url(key)
    auto_url: str | None = None
    if plan:
        plan_name, want_url = plan
        print(f"套餐识别 : {plan_name}")
        if base_url.rstrip("/") == want_url.rstrip("/"):
            print(f"Base URL : {base_url}  √ 与套餐配套")
        else:
            auto_url = want_url
            print("Base URL : 与套餐**不配套**，已自动改用该套餐专属地址：")
            print(f"           原填 {base_url}")
            print(f"           改用 {want_url}")
            base_url = want_url
    else:
        print("套餐识别 : 前缀不在已知套餐表内，沿用配置中的 Base URL")
        print(f"Base URL : {base_url}")
    print(f"model    : {model}")
    print("-" * 68)

    warns = inspect(key, base_url)
    if warns:
        print("格式体检：发现可疑之处")
        for w in warns:
            print(f"  ! {w}")
    else:
        print("格式体检：通过（形如标准 API Key）")
    print("-" * 68)

    if args.no_probe:
        print("已按 --no-probe 跳过联网验证。")
        ok = True
    else:
        print("正在实测（向 base_url 发一次最小请求）...")
        ok, msg = probe(base_url, model, key)
        print(("  OK  " if ok else "  失败 ") + msg)

    if not ok and not warnings_are_fatal(warns):
        # 实测失败但格式正常：仍可能是限流/网络问题，交给用户决定
        print("  （格式正常但实测失败，可能是限流/欠费/网络问题，请核对平台控制台）")

    if not ok:
        print("-" * 68)
        print("未通过验证，**不写入** .env。")
        print("排查顺序：① Key 前缀是否为小写 sk-；② Base URL 是否与套餐配套（上面已自动帮你配对）；")
        print("          ③ 模型名是否在该套餐的可用列表内（可 GET <base_url>/models 查看）。")
        return 1

    if args.check:
        print("-" * 68)
        print("验证通过；按 --check 要求未写入 .env。")
        return 0

    _, note = write_dotenv(key, auto_url)
    print("-" * 68)
    print(note)
    print("提醒：已在运行的 --serve 进程不会热加载，需要重启服务才生效。")
    print("=" * 68)
    return 0


def warnings_are_fatal(warns: list[str]) -> bool:
    """格式警告是否属于「基本可以断定是错的东西」。"""
    return any("签名令牌" in w or "过短" in w or "为空" in w for w in warns)


if __name__ == "__main__":
    raise SystemExit(main())
