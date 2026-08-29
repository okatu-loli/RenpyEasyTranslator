import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from tqdm import tqdm


DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"
DEFAULT_LANGUAGE = "Simplified Chinese"
DEFAULT_MODEL = "auto"
DEFAULT_CHUNK_BLOCKS = 4
DEFAULT_TIMEOUT = 120
DEFAULT_RETRIES = 3
DEFAULT_TEMPERATURE = 0.7
DEFAULT_TOP_P = 0.6
DEFAULT_MAX_TOKENS = 1024

# Ren'Py text tags/interpolation, escaped sequences and printf-style placeholders.
# These tokens must never be translated or modified.
PROTECTED_TOKEN_RE = re.compile(
    r"(\{/?[A-Za-z][^{}]*\}|\[[^\[\]\n]+\]|%(?:\([^)]+\))?[#0\- +]?(?:\d+|\*)?(?:\.\d+|\.\*)?[diouxXeEfFgGcrs%]|\\[nrt\"\\])"
)

# Translation-template string lines such as:
#     "Hello"
#     e "Hello"
#     old "Hello"
#     new "Hello"
QUOTED_RE = re.compile(
    r'^(?P<prefix>\s*(?:old\s+|new\s+|[A-Za-z_][\w.]*\s+)?)"(?P<text>(?:\\.|[^"\\])*)"(?P<suffix>\s*)$'
)

CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")


def http_json(method, url, payload=None, timeout=DEFAULT_TIMEOUT):
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"无法连接 LM Studio：{e}") from e


def normalize_base_url(url):
    url = url.rstrip("/")
    if url.endswith("/v1"):
        return url
    return url + "/v1"


def detect_model(base_url, requested):
    if requested and requested.lower() != "auto":
        return requested
    result = http_json("GET", f"{base_url}/models")
    models = result.get("data", [])
    if not models:
        raise RuntimeError(
            "LM Studio /v1/models 没有返回已加载模型，请先在 LM Studio 中加载模型并启动 Local Server。"
        )
    return models[0]["id"]


def decode_renpy_string(text):
    # Deliberately conservative: unicode_escape would corrupt non-ASCII text.
    return text.replace(r'\"', '"').replace(r"\\", "\\")


def encode_renpy_string(text):
    return (
        text.replace("\\", r"\\")
        .replace('"', r'\"')
        .replace("\r", "")
        .replace("\n", r"\n")
    )


def is_source_line(line):
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return False
    m = QUOTED_RE.match(line)
    if not m:
        return False
    return m.group("prefix").strip() != "old"


def collect_entries(lines):
    entries = []
    for idx, line in enumerate(lines):
        m = QUOTED_RE.match(line)
        if not m or not is_source_line(line):
            continue

        prefix = m.group("prefix").strip()
        text = decode_renpy_string(m.group("text"))
        if not text.strip():
            continue

        # Generated old/new string blocks: translate the source from old, but only
        # when new has not already been edited.
        if prefix == "new":
            previous_old = None
            for j in range(idx - 1, max(-1, idx - 5), -1):
                pm = QUOTED_RE.match(lines[j])
                if pm and pm.group("prefix").strip() == "old":
                    previous_old = decode_renpy_string(pm.group("text"))
                    break
            if previous_old is not None:
                if text.strip() and text != previous_old:
                    continue
                text = previous_old

        # Allows safe resume after an interrupted run: lines already containing
        # Chinese are treated as translated and are not sent through the model again.
        if CJK_RE.search(text):
            continue

        entries.append(
            {
                "line": idx,
                "prefix": m.group("prefix"),
                "suffix": m.group("suffix"),
                "text": text,
            }
        )
    return entries


def make_placeholders(text):
    """Replace protected Ren'Py tokens with simple ASCII placeholders."""
    table = []

    def repl(match):
        marker = f"__RET_{len(table):04d}__"
        table.append((marker, match.group(0)))
        return marker

    return PROTECTED_TOKEN_RE.sub(repl, text), table


def clean_model_output(content):
    content = str(content).strip()

    if content.startswith("```"):
        content = re.sub(r"^```(?:text|txt|json)?\s*", "", content, flags=re.I)
        content = re.sub(r"\s*```$", "", content)
        content = content.strip()

    # Some models add a short translation label despite being told not to.
    content = re.sub(
        r"^(?:翻译(?:结果|如下)?|译文|简体中文|中文译文)\s*[:：]\s*",
        "",
        content,
        flags=re.I,
    ).strip()

    # If the model returned a JSON-style quoted string, decode just that string.
    if len(content) >= 2 and content[0] == '"' and content[-1] == '"':
        try:
            decoded = json.loads(content)
            if isinstance(decoded, str):
                content = decoded
        except json.JSONDecodeError:
            pass

    return content.strip()


def call_translation_model(
    base_url,
    model,
    text,
    target_language,
    timeout,
    retries,
    temperature,
    top_p,
    max_tokens,
    placeholder_note=False,
):
    note = ""
    if placeholder_note:
        note = (
            " 文本中的 __RET_0000__ 这类标记是不可修改的占位符，必须逐字原样保留，"
            "不要删除、翻译、移动或改变下划线和数字。"
        )

    prompt = (
        f"将以下英文文本翻译为{target_language}。"
        "只输出译文，不要解释，不要添加引号。"
        "保持人物语气、俚语、粗口、性暗示及虚构成人对白的原意，不要弱化措辞。"
        + note
        + "\n\n"
        + text
    )

    # HY-MT is a translation model rather than a general instruction-following
    # model. A single plain translation request is much more reliable than asking
    # it to manufacture JSON arrays.
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "stream": False,
    }

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            result = http_json(
                "POST", f"{base_url}/chat/completions", payload, timeout=timeout
            )
            content = result["choices"][0]["message"]["content"]
            translated = clean_model_output(content)
            if not translated:
                raise ValueError("模型返回了空译文")
            return translated
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(min(2 ** (attempt - 1), 5))

    raise RuntimeError(f"翻译失败，重试 {retries} 次后仍出错：{last_error}")


def translate_piece(
    base_url,
    model,
    piece,
    target_language,
    timeout,
    retries,
    temperature,
    top_p,
    max_tokens,
):
    # Keep exact surrounding whitespace when translating fallback segments.
    m = re.match(r"^(\s*)(.*?)(\s*)$", piece, flags=re.S)
    leading, core, trailing = m.groups()
    if not core or not LATIN_RE.search(core):
        return piece

    translated = call_translation_model(
        base_url,
        model,
        core,
        target_language,
        timeout,
        retries,
        temperature,
        top_p,
        max_tokens,
        placeholder_note=False,
    )
    return leading + translated + trailing


def translate_by_segments(
    base_url,
    model,
    text,
    target_language,
    timeout,
    retries,
    temperature,
    top_p,
    max_tokens,
):
    """
    Guaranteed-safe fallback.

    Protected tags/variables are never sent to the model. Only the text between
    them is translated, then the original tokens are concatenated back verbatim.
    This may be slightly less context-aware, but it cannot lose {i}, {/i}, [name],
    printf placeholders, or escaped Ren'Py sequences.
    """
    parts = PROTECTED_TOKEN_RE.split(text)
    out = []
    for part in parts:
        if not part:
            continue
        if PROTECTED_TOKEN_RE.fullmatch(part):
            out.append(part)
        else:
            out.append(
                translate_piece(
                    base_url,
                    model,
                    part,
                    target_language,
                    timeout,
                    retries,
                    temperature,
                    top_p,
                    max_tokens,
                )
            )
    return "".join(out)


def translate_text(
    base_url,
    model,
    text,
    target_language,
    timeout,
    retries,
    temperature,
    top_p,
    max_tokens,
):
    protected, table = make_placeholders(text)

    translated = call_translation_model(
        base_url,
        model,
        protected,
        target_language,
        timeout,
        retries,
        temperature,
        top_p,
        max_tokens,
        placeholder_note=bool(table),
    )

    # Fast path: the model preserved every marker exactly once.
    if table and not all(translated.count(marker) == 1 for marker, _ in table):
        return translate_by_segments(
            base_url,
            model,
            text,
            target_language,
            timeout,
            retries,
            temperature,
            top_p,
            max_tokens,
        )

    for marker, original in table:
        translated = translated.replace(marker, original)

    return translated


def find_rpy_files(path):
    return sorted(Path(path).rglob("*.rpy"))


def load_finished(path):
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def append_finished(path, file_path):
    with path.open("a", encoding="utf-8") as f:
        f.write(str(file_path.resolve()) + "\n")


def process_file(file_path, args, model, finished_path, finished):
    resolved = str(file_path.resolve())
    if resolved in finished:
        print(f"跳过已完成：{file_path}")
        return

    original = file_path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=False)
    entries = collect_entries(lines)
    if not entries:
        print(f"无待翻译文本，跳过：{file_path}")
        append_finished(finished_path, file_path)
        return

    backup_path = file_path.with_suffix(file_path.suffix + ".bak")
    if args.backup and not backup_path.exists():
        backup_path.write_text(original, encoding="utf-8")

    # --batch now controls checkpoint granularity only. Each dialogue line is sent
    # as its own translation request because HY-MT is substantially more reliable
    # this way than with a JSON batch protocol.
    groups = range(0, len(entries), args.batch)
    for start in tqdm(groups, desc=file_path.name, unit="batch"):
        batch = entries[start : start + args.batch]

        for entry in batch:
            translated = translate_text(
                args.base_url,
                model,
                entry["text"],
                args.target_language,
                args.timeout,
                args.retries,
                args.temperature,
                args.top_p,
                args.max_tokens,
            )
            escaped = encode_renpy_string(translated)
            lines[entry["line"]] = (
                f'{entry["prefix"]}"{escaped}"{entry["suffix"]}'
            )

        # Incremental checkpoint so Ctrl+C or a model error does not lose progress.
        file_path.write_text(
            "\n".join(lines) + ("\n" if original.endswith("\n") else ""),
            encoding="utf-8",
        )

    append_finished(finished_path, file_path)
    print(f"处理完成：{file_path}")


def parse_args():
    p = argparse.ArgumentParser(
        description="Ren'Py rpy 本地 AI 一键翻译（LM Studio / OpenAI Compatible API）"
    )
    p.add_argument(
        "path", help="Ren'Py 生成的翻译目录，例如 game/tl/schinese"
    )
    p.add_argument(
        "--base-url",
        default=os.getenv("LMSTUDIO_BASE_URL", DEFAULT_BASE_URL),
        help="LM Studio API，默认 http://127.0.0.1:1234/v1",
    )
    p.add_argument(
        "--model",
        default=os.getenv("LMSTUDIO_MODEL", DEFAULT_MODEL),
        help="模型 id；默认 auto 自动取 /v1/models 第一个模型",
    )
    p.add_argument("--target-language", default=DEFAULT_LANGUAGE)
    p.add_argument(
        "--batch",
        type=int,
        default=DEFAULT_CHUNK_BLOCKS,
        help="每多少条翻译写回一次文件，默认 4；每条文本仍独立请求模型",
    )
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    p.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    p.add_argument("--top-p", type=float, default=DEFAULT_TOP_P)
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    p.add_argument(
        "--no-backup",
        dest="backup",
        action="store_false",
        help="不创建 .rpy.bak 备份",
    )
    p.set_defaults(backup=True)
    return p.parse_args()


def main():
    args = parse_args()
    if args.batch < 1:
        print("--batch 必须 >= 1", file=sys.stderr)
        return 2

    args.base_url = normalize_base_url(args.base_url)
    root = Path(args.path)
    if not root.exists():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 2

    model = detect_model(args.base_url, args.model)
    print(f"LM Studio: {args.base_url}")
    print(f"模型: {model}")
    print(f"翻译目录: {root.resolve()}")

    finished_path = Path("finished_file_list_lmstudio.txt")
    finished = load_finished(finished_path)
    files = find_rpy_files(root)
    if not files:
        print("没有找到 .rpy 文件。")
        return 1

    for file_path in files:
        try:
            process_file(file_path, args, model, finished_path, finished)
        except KeyboardInterrupt:
            print("\n用户中止。已完成的批次已写回文件。")
            return 130
        except Exception as e:
            with Path("error_file_list_lmstudio.txt").open(
                "a", encoding="utf-8"
            ) as f:
                f.write(f"{file_path.resolve()}\t{e}\n")
            print(f"处理失败：{file_path}\n{e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
