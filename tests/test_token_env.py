# -*- coding: utf-8 -*-
"""凭据文件（.env）读写与 Token 界面共用的那份清单。

## 为什么要有这个测试

新增的「填写云端 OCR Token」界面会**改写用户手写的 .env**。这类"顺手把一个
配置文件重写一遍"的动作最容易出的事故有三种，本测试逐个钉住：

  1. 把注释和用户自己加的键冲掉 —— 用户下一次打开文件就懵了；
  2. 值里带 `#` 或空格时不加引号 —— 读回来被截断，然后 401，
     而界面上看起来"填对了"；
  3. 同一个键在文件里出现两次（`export FOO=1` 那种写法没被识别到时
     会在末尾再追加一行）—— 后写的那行才生效，前一行成了幽灵。

另外锁住「界面能填的键 = 命令行认得的键」：两侧共用 config.CRED_FIELDS，
一旦有人只改一侧，这里立刻会红。

## 用法

    python tests/test_token_env.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from doc2md.config import (          # noqa: E402
    CRED_FIELDS,
    _parse_env_text,
    any_token_filled,
    cred_field,
    cred_key_list,
    describe_tokens,
    dismiss_token_prompt,
    env_template_text,
    filled_aliases,
    load_env_file,
    save_env_values,
    token_prompt_dismissed,
)

LOG_PATH = Path(__file__).with_name("test_token_env.log")

FAILURES = 0


class _Tee:
    def __init__(self, stream, path: Path):
        self._stream = stream
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = open(path, "w", encoding="utf-8")

    def write(self, s):
        self._stream.write(s)
        self._file.write(s)
        return len(s)

    def flush(self):
        self._stream.flush()
        self._file.flush()


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILURES
    if cond:
        print(f"  [PASS] {name}")
    else:
        FAILURES += 1
        print(f"  [FAIL] {name}  {detail}")


# 本测试全程用合成键名，绝不碰真实 Token；需要动 os.environ 的地方都成对恢复。
K1 = "DOC2MD_TEST_ALPHA"
K2 = "DOC2MD_TEST_BETA"
K3 = "DOC2MD_TEST_GAMMA"


SAMPLE = """\
# 云端 OCR 凭据文件
# 手写的注释不能被冲掉

DOC2MD_TEST_ALPHA=old-alpha
export DOC2MD_TEST_BETA=old-beta   # 行尾注释

# 用户自己加的、本工具不认识的键
MY_OWN_SETTING=keep-me
"""


# ============================================================================
#  1. 保留注释 / 顺序 / 未知键
# ============================================================================
def t_preserve(base: Path) -> None:
    print("\n[1] 改写时保留注释、顺序与未知键")
    p = base / ".env"
    p.write_text(SAMPLE, encoding="utf-8")

    save_env_values({K1: "new-alpha"}, p)
    text = p.read_text(encoding="utf-8")

    check("注释还在", "# 手写的注释不能被冲掉" in text)
    check("未知键还在", "MY_OWN_SETTING=keep-me" in text)
    check("键名顺序没变（ALPHA 仍在 BETA 之前）",
          text.index(K1) < text.index(K2))
    check("原地替换而不是追加", text.count(f"{K1}=") == 1,
          f"出现 {text.count(K1 + '=')} 次")
    check("值已更新", "DOC2MD_TEST_ALPHA=new-alpha" in text)

    parsed = _parse_env_text(text)
    check("读回来与写进去一致", parsed.get(K1) == "new-alpha", repr(parsed.get(K1)))
    check("export 前缀的行被识别为同一个键（不会重复追加）",
          text.count(K2) == 1 and parsed.get(K2) == "old-beta",
          f"count={text.count(K2)} value={parsed.get(K2)!r}")
    check("没有留下 .tmp", not (p.with_name(p.name + ".tmp")).exists())


# ============================================================================
#  2. 需要引号的值
# ============================================================================
def t_quoting(base: Path) -> None:
    print("\n[2] 值里含 # 或首尾空格时必须加引号（否则读回来会被截断）")
    p = base / "quote.env"
    cases = {
        K1: "abc#def",            # 含 #：不加引号会被当成注释起点
        K2: "  padded  ",         # 首尾空格：不加引号会被 strip 掉
        K3: 'has"quote',          # 含双引号：必须换单引号包
    }
    save_env_values(cases, p)
    parsed = _parse_env_text(p.read_text(encoding="utf-8"))
    for key, want in cases.items():
        check(f"{key} 往返无损", parsed.get(key) == want.strip(),
              f"写入 {want!r} → 读回 {parsed.get(key)!r}")

    # 行尾注释仍然只是注释
    p2 = base / "tilde.env"
    save_env_values({K1: "plain"}, p2)
    save_env_values({K2: "has # hash"}, p2)
    parsed2 = _parse_env_text(p2.read_text(encoding="utf-8"))
    check("普通值不被加多余引号", parsed2.get(K1) == "plain")
    check("含 # 的值不会被当成注释截断", parsed2.get(K2) == "has # hash",
          repr(parsed2.get(K2)))


# ============================================================================
#  3. 置空与删除
# ============================================================================
def t_clear_and_delete(base: Path) -> None:
    print("\n[3] 置空 / 删除")
    p = base / "clear.env"
    p.write_text(SAMPLE, encoding="utf-8")

    save_env_values({K1: ""}, p)                 # 空串 → 写成 KEY=，行还在
    text = p.read_text(encoding="utf-8")
    check("置空后行仍存在且值为空",
          f"{K1}=" in text and _parse_env_text(text).get(K1) in ("", None))
    check("置空不影响其它键", "old-beta" in text)

    save_env_values({K2: None}, p)               # None → 整行删掉
    text = p.read_text(encoding="utf-8")
    check("删除后键完全消失", K2 not in text)
    check("删除只删该行，注释与未知键照旧",
          "# 手写的注释不能被冲掉" in text and "MY_OWN_SETTING=keep-me" in text)


# ============================================================================
#  4. 立即生效（不必重启进程）
# ============================================================================
def t_apply_now(base: Path) -> None:
    print("\n[4] 写完当场灌进 os.environ（不用重启程序）")
    p = base / "apply.env"
    p.write_text(f"{K1}=\n", encoding="utf-8")
    saved = {k: os.environ.get(k) for k in (K1, K2)}
    try:
        os.environ.pop(K1, None)
        os.environ.pop(K2, None)

        save_env_values({K1: "fresh-value"}, p)
        check("有值的键立刻进 os.environ", os.environ.get(K1) == "fresh-value",
              repr(os.environ.get(K1)))

        save_env_values({K1: ""}, p)
        check("置空的键立刻从 os.environ 摘掉", K1 not in os.environ,
              f"仍是 {os.environ.get(K1)!r}")

        # 与读的一侧闭环：重新加载文件后仍然是空
        load_env_file(p, force=True)
        check("重新加载后不复活", not (os.environ.get(K1) or "").strip())
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ============================================================================
#  5. 模板与键清单同源
# ============================================================================
def t_template_and_keys(base: Path) -> None:
    print("\n[5] 模板与键清单相互一致")
    text = env_template_text()
    keys_in_template = {k for k, _ in _parse_env_text(text).items()}
    canonical = {f.key for f in CRED_FIELDS}
    check("模板覆盖全部规范键", canonical <= keys_in_template,
          f"缺 {sorted(canonical - keys_in_template)}")
    check("模板里没有多余的键", not (keys_in_template - canonical),
          f"多出 {sorted(keys_in_template - canonical)}")
    check("模板里每个键的值都是空的（不会写出假 Token）",
          all(_parse_env_text(text)[k] == "" for k in keys_in_template))

    listed = {k for k, _ in cred_key_list()}
    check("自检清单覆盖全部规范键", canonical <= listed,
          f"缺 {sorted(canonical - listed)}")
    for f in CRED_FIELDS:
        check(f"别名 {f.aliases or '（无）'} 都在清单里",
              all(a in listed for a in f.aliases))

    check("按别名能反查到字段", cred_field("MINERU_TOKEN") is not None
          and cred_field("MINERU_TOKEN").key == "DOC2MD_MINERU_TOKEN")
    check("未知键反查为 None", cred_field("NOT_A_KEY") is None)

    # 描述函数不能崩，且不许泄露 Token 明文
    secret = "sk-" + "x" * 40
    saved = os.environ.get(CRED_FIELDS[0].key)
    try:
        os.environ[CRED_FIELDS[0].key] = secret
        desc = describe_tokens()
        check("填写情况里不含 Token 明文", secret not in desc, desc)
        check("填写情况能反映已填", "已填" in desc, desc)
    finally:
        if saved is None:
            os.environ.pop(CRED_FIELDS[0].key, None)
        else:
            os.environ[CRED_FIELDS[0].key] = saved


# ============================================================================
#  6. 文件不存在时能新建
# ============================================================================
def t_create_new(base: Path) -> None:
    print("\n[6] 文件不存在时新建（装机后第一次填 Token 就走这条路）")
    p = base / "nested" / "sub" / ".env"
    check("目标文件确实不存在", not p.exists())
    save_env_values({K1: "v1", K2: "v2"}, p)
    check("父目录被自动创建", p.is_file())
    parsed = _parse_env_text(p.read_text(encoding="utf-8"))
    check("两个键都写进去了", parsed == {K1: "v1", K2: "v2"}, repr(parsed))


def t_first_run_prompt_mark(base: Path) -> None:
    """「首次启动提示填 Token」的判定与「以后再说」记号。

    判定条件用的是「常用 Token 一个都没填」，不是「文件不存在」——
    装机包会把 .env.example 复制成一份**空的 .env**，按文件存在与否判断
    就永远不会弹窗，用户只能自己猜到哪里填（这条踩过）。
    """
    print("\n[7] 首次启动的弹窗判定与「以后再说」记号")
    p = base / "prompt.env"
    keys = [f.key for f in CRED_FIELDS if not f.advanced]
    saved = {k: os.environ.get(k) for k in (*keys, "DOC2MD_VLM_TOKEN")}
    try:
        for k in saved:
            os.environ.pop(k, None)

        # 文件存在、但三项都空着 → 应当提示
        p.write_text("".join(f"{k}=\n" for k in keys), encoding="utf-8")
        check("空的 .env（装机后的样子）→ 判定为还没填过", not any_token_filled())
        check("还没说过「以后再说」", not token_prompt_dismissed(p))

        dismiss_token_prompt(p)
        text = p.read_text(encoding="utf-8")
        check("记号写了进去", token_prompt_dismissed(p))
        check("记号是注释形态，不会被当成键",
              "DOC2MD_TOKEN_PROMPT" not in _parse_env_text(text))
        check("原有的键一行没少", all(f"{k}=" in text for k in keys))

        n = dismiss_token_prompt(p).read_text(encoding="utf-8")
        check("重复调用不会写第二条", n.count("# DOC2MD_TOKEN_PROMPT=skipped") == 1)

        # 填上一个就算填过了
        save_env_values({keys[0]: "some-token"}, p)
        check("填了一项之后不再提示", any_token_filled())

        # 空文件也能打记号（不额外造别的文件）
        p2 = base / "brand-new.env"
        dismiss_token_prompt(p2)
        check("目标文件不存在时会连文件一起建出来",
              p2.is_file() and token_prompt_dismissed(p2))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main() -> int:
    sys.stdout = _Tee(sys.stdout, LOG_PATH)
    sys.stderr = sys.stdout

    print("=" * 74)
    print("  凭据文件（.env）读写测试")
    print("=" * 74)

    with tempfile.TemporaryDirectory(prefix="doc2md_env_", ignore_cleanup_errors=True) as d:
        base = Path(d)
        t_preserve(base)
        t_quoting(base)
        t_clear_and_delete(base)
        t_apply_now(base)
        t_template_and_keys(base)
        t_create_new(base)
        t_first_run_prompt_mark(base)

    print()
    print("=" * 74)
    print(f"  失败项：{FAILURES}")
    print(f"  日志：{LOG_PATH}")
    print("=" * 74)
    sys.stdout.flush()
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
