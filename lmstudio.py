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
DEFAULT_MAX_TOKENS = 2048

# Ren'Py text tags/interpolation and common printf-style placeholders.
PROTECTED_TOKEN_RE = re.compile(
    r"(\{/?[A-Za-z][^{}]*\}|\[[^\[\]\n]+\]|%(?:\([^)]+\))?[#0\- +]?(?:\d+|\*)?(?:\.\d+|\.\*)?[diouxXeEfFgGcrs%])"
)

# String literals in translation templates. We translate only the contents of lines
# that look like Ren'Py dialogue/string entries, leaving comments/code untouched.
QUOTED_RE = re.compile(r'^(?P<prefix>\s*(?:old\s+|new\s+|[A-Za-z_][\w.]*\s+)?)"(?P<text>(?:\\.|[^"\\])*)"(?P<suffix>\s*)$')


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
        raise RuntimeError("LM Studio /v1/models 没有返回已加载模型，请先在 LM Studio 中加载模型并启动 Local Server。")
    return models[0]["id"]


def protect_tokens(text):
    table = []

    def repl(match):
        token = f"⟦RET_TOKEN_{len(table):04d}⟧"
        table.append((token, match.group(0)))
        return token

    return PROTECTED_TOKEN_RE.sub(repl, text), table


def restore_tokens(text, table):
    for placeholder, original in table:
        text = text.replace(placeholder, original)
    return text


def decode_renpy_string(text):
    # Keep this deliberately conservative. Ren'Py translation templates are usually
    # ordinary escaped strings; unicode_escape would corrupt non-ASCII text.
    return text.replace(r'\"', '"').replace(r"\\", "\\")


def encode_renpy_string(text):
    return text.replace("\\", r"\\").replace('"', r'\"').replace("\r", "").replace("\n", r"\n")


def is_source_line(line):
    stripped = line.lstrip()
    if stripped.startswith("#"):
        return False
    m = QUOTED_RE.match(line)
    if not m:
        return False
    prefix = m.group("prefix").strip()
    # In Ren'Py generated tl files, old lines are source strings and new lines are
    # destination strings. We should not translate the old source line itself.
    return prefix != "old"


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
        # Generated string translation blocks use old/new pairs. Translate only new
        # lines when they still equal the source text or are blank.
        if prefix == "new":
            previous_old = None
            for j in range(idx - 1, max(-1, idx - 5), -1):
                pm = QUOTED_RE.match(lines[j])
                if pm and pm.group("prefix").strip() == "old":
                    previous_old = decode_renpy_string(pm.group("text"))
                    break
            if previous_old is not None:
                if text.strip() and text != previous_old:
                    # Looks already translated/edited; preserve it.
                    continue
                text = previous_old
        entries.append({"line": idx, "prefix": m.group("prefix"), "suffix": m.group("suffix"), "text": text})
    return entries


def build_prompt(items, target_language):
    records = []
    token_tables = {}
    for i, item in enumerate(items):
        protected, table = protect_tokens(item["text"])
        token_tables[str(i)] = table
        records.append({"id": i, "text": protected})

    system = (
        "You are a professional visual novel and Ren'Py game translator. "
        "Translate English dialogue and narration into natural, fluent Simplified Chinese. "
        "Preserve character voice, slang, jokes, profanity, sexual implications, explicit fictional adult dialogue, "
        "and emotional tone without censorship or euphemistic softening. "
        "Never translate, remove, reorder, or alter placeholders such as ⟦RET_TOKEN_0000⟧. "
        "Return valid JSON only, with exactly the same ids and one translated text for each id. "
        "Do not add explanations."
    )
    user = (
        f"Target language: {target_language}.\n"
        "Translate the following JSON array. Return exactly an array of objects in the form "
        '[{"id":0,"text":"..."}].\n\n' + json.dumps(records, ensure_ascii=False)
    )
    return system, user, token_tables


def parse_json_response(content):
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.I)
        content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("[")
        end = content.rfind("]")
        if start >= 0 and end > start:
            return json.loads(content[start : end + 1])
        raise


def translate_batch(base_url, model, items, target_language, timeout, retries, temperature, top_p, max_tokens):
    system, user, token_tables = build_prompt(items, target_language)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens,
        "stream": False,
    }

    last_error = None
    for attempt in range(1, retries + 1):
        try:
            result = http_json("POST", f"{base_url}/chat/completions", payload, timeout=timeout)
            content = result["choices"][0]["message"]["content"]
            parsed = parse_json_response(content)
            if not isinstance(parsed, list) or len(parsed) != len(items):
                raise ValueError(f"模型返回数量不匹配：期望 {len(items)}，实际 {len(parsed) if isinstance(parsed, list) else '非数组'}")
            by_id = {str(x["id"]): x["text"] for x in parsed}
            translated = []
            for i in range(len(items)):
                key = str(i)
                if key not in by_id:
                    raise ValueError(f"模型返回缺少 id={i}")
                text = restore_tokens(str(by_id[key]), token_tables[key])
                # Ensure every protected token survived exactly once at minimum.
                for _, original in token_tables[key]:
                    if original not in text:
                        raise ValueError(f"占位符/文本标签丢失：{original}")
                translated.append(text)
            return translated
        except Exception as e:
            last_error = e
            if attempt < retries:
                time.sleep(min(2 ** (attempt - 1), 5))
    raise RuntimeError(f"翻译失败，重试 {retries} 次后仍出错：{last_error}")


def find_rpy_files(path):
    return sorted(Path(path).rglob("*.rpy"))


def load_finished(path):
    finished = set()
    if path.exists():
        finished = {line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}
    return finished


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

    for start in tqdm(range(0, len(entries), args.batch), desc=file_path.name, unit="batch"):
        batch = entries[start : start + args.batch]
        try:
            results = translate_batch(
                args.base_url,
                model,
                batch,
                args.target_language,
                args.timeout,
                args.retries,
                args.temperature,
                args.top_p,
                args.max_tokens,
            )
        except Exception:
            # If a multi-entry batch fails validation, retry one by one. This is slower
            # but prevents one malformed response from corrupting an entire rpy file.
            if len(batch) == 1:
                raise
            results = []
            for entry in batch:
                results.extend(
                    translate_batch(
                        args.base_url,
                        model,
                        [entry],
                        args.target_language,
                        args.timeout,
                        args.retries,
                        args.temperature,
                        args.top_p,
                        args.max_tokens,
                    )
                )

        for entry, translated in zip(batch, results):
            escaped = encode_renpy_string(translated)
            lines[entry["line"]] = f'{entry["prefix"]}"{escaped}"{entry["suffix"]}'

        # Incremental save: a crash or Ctrl+C will not lose completed batches.
        file_path.write_text("\n".join(lines) + ("\n" if original.endswith("\n") else ""), encoding="utf-8")

    append_finished(finished_path, file_path)
    print(f"处理完成：{file_path}")


def parse_args():
    p = argparse.ArgumentParser(description="Ren'Py rpy 本地 AI 一键翻译（LM Studio / OpenAI Compatible API）")
    p.add_argument("path", help="Ren'Py 生成的翻译目录，例如 game/tl/schinese")
    p.add_argument("--base-url", default=os.getenv("LMSTUDIO_BASE_URL", DEFAULT_BASE_URL), help="LM Studio API，默认 http://127.0.0.1:1234/v1")
    p.add_argument("--model", default=os.getenv("LMSTUDIO_MODEL", DEFAULT_MODEL), help="模型 id；默认 auto 自动取 /v1/models 第一个模型")
    p.add_argument("--target-language", default=DEFAULT_LANGUAGE)
    p.add_argument("--batch", type=int, default=DEFAULT_CHUNK_BLOCKS, help="每次请求翻译多少条文本，默认 4")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    p.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    p.add_argument("--top-p", type=float, default=DEFAULT_TOP_P)
    p.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    p.add_argument("--no-backup", dest="backup", action="store_false", help="不创建 .rpy.bak 备份")
    p.set_defaults(backup=True)
    return p.parse_args()


def main():
    args = parse_args()
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
            with Path("error_file_list_lmstudio.txt").open("a", encoding="utf-8") as f:
                f.write(f"{file_path.resolve()}\t{e}\n")
            print(f"处理失败：{file_path}\n{e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
