# Renpy游戏rpy一键汉化工具

鉴于目前的Renpy汉化工具大多都要生成各种乱七八糟的奇怪文件导来导去，在试了各种工具之后厌倦了这种不优雅的汉化行为。

本 fork 新增了 **LM Studio / OpenAI Compatible API 本地 AI 翻译**，原来的 DeepL 网页翻译流程仍保留在 `main.py` 中。

## 使用

### 1、生成翻译文件

* 确保game文件夹里有.rpy后缀的脚本文件
  * 如果没有的话，就使用UnRen等软件解包.rpa文件/反编译.rpyc文件。

* 启动Renpy引擎，并生成翻译文件：

  * 选择你想翻译的游戏（如果工程列表里没有，就在设置里重新设置一下工程目录）

  * 点击生成翻译文件
  * **千万不要勾选“为翻译生成空字串”这一栏**

  * 点击生成翻译文件

生成完成后，一般会得到：

```text
game/tl/schinese/
```

### 2A、推荐：LM Studio 本地 AI 翻译

新增脚本：

```text
lmstudio.py
```

特点：

* 不需要 Chrome / Selenium / DeepL
* 直接调用 LM Studio 本地 OpenAI 兼容 API
* 自动读取 `/v1/models` 获取当前已加载模型
* 默认备份原 `.rpy` 为 `.rpy.bak`
* 增量写入，程序中断后已完成批次不会全部丢失
* 默认跳过已经含中文的译文，重复运行不会把已经翻好的中文再次改写
* 支持 `--retranslate`，需要时可从 Ren'Py 注释/`old` 行恢复英文原文并重翻已有中文
* 自动保护 Ren'Py 文本标签、变量和常见占位符，例如 `{i}`、`{/i}`、`[player_name]`、`%(name)s`
* 默认多句批量翻译，同一批文本天然互为上下文，减少 API 往返并提高速度
* 可额外携带前面若干条英中对照作为上下文
* 批量协议或标签保护失败时自动降级为安全的单句翻译，不会因为一条异常污染整个文件
* Prompt 针对 Visual Novel / Ren'Py 对话进行了优化，保留人物语气、粗口、性暗示及虚构成人对白原意

#### LM Studio 设置

1. 在 LM Studio 中加载模型。
2. 打开 Local Server / Developer Server。
3. 默认地址：

```text
http://127.0.0.1:1234
```

推荐用于英文 Visual Novel → 简体中文的模型，例如：

```text
Huihui-HY-MT1.5-7B-abliterated GGUF
```

#### 安装 Python 依赖

`lmstudio.py` 仅额外使用 `tqdm`，HTTP 请求使用 Python 标准库：

```bash
pip install tqdm
```

#### 运行

假设 Ren'Py 生成的翻译目录为：

```text
D:\Games\WaifuAcademy\game\tl\schinese
```

运行：

```bash
python lmstudio.py "D:\Games\WaifuAcademy\game\tl\schinese"
```

脚本会自动连接 `http://127.0.0.1:1234/v1`，并自动使用 LM Studio 当前加载的第一个模型。

#### 推荐参数

当前默认：

```text
batch = 8
context = 4
temperature = 0.7
top_p = 0.6
max_tokens = 2048
```

这里的 `batch=8` 表示尽量一次 API 请求翻译 8 条连续文本；这些文本会彼此作为上下文。如果模型没有按协议返回，会自动降级为单句，因此无需为了稳定性长期固定在 `batch=1`。

Waifu Academy / 7B 本地模型可先尝试：

```bash
python lmstudio.py "D:\Games\WaifuAcademy\game\tl\schinese" --batch 8 --context 4
```

如果显卡和模型速度足够，进一步提速可以尝试：

```bash
python lmstudio.py "D:\Games\WaifuAcademy\game\tl\schinese" --batch 12 --context 6
```

如果批量经常自动降级，则改小：

```bash
python lmstudio.py "D:\Games\WaifuAcademy\game\tl\schinese" --batch 4 --context 4
```

`--context 0` 可以关闭额外的历史英中对照；当前批次中的多条文本仍然互相提供语境。

#### 已经翻译过的内容

默认模式是 **增量翻译**：

* 已经含中文的目标行保持不变；
* 中途停止后直接重新运行即可；
* 已经完整完成并记录在 `finished_file_list_lmstudio.txt` 的文件会被整体跳过。

如果你更换模型或 Prompt，希望重新润色已经翻好的中文，可以使用：

```bash
python lmstudio.py "D:\Games\WaifuAcademy\game\tl\schinese" --retranslate --batch 8 --context 6
```

`--retranslate` 会尽量从 Ren'Py 自动保留的英文注释行或 `old` 行恢复原文，再重新生成中文。找不到可靠英文原文的行不会强行改写。

#### 其他参数

手动指定模型：

```bash
python lmstudio.py "D:\Games\WaifuAcademy\game\tl\schinese" --model "你的模型ID"
```

指定其他 OpenAI 兼容 API：

```bash
python lmstudio.py "D:\Games\WaifuAcademy\game\tl\schinese" --base-url http://127.0.0.1:1234/v1
```

查看完整参数：

```bash
python lmstudio.py -h
```

#### 进度与错误文件

成功完成的文件会记录在：

```text
finished_file_list_lmstudio.txt
```

失败记录：

```text
error_file_list_lmstudio.txt
```

`.rpy.bak` 是首次处理该文件时保存的原始备份。

### 2B、原版 DeepL 网页翻译

* 在 `main.py` 的 main 函数里面输入你生成的翻译文件目录。
* 最新版本 Selenium 不再需要手动下载 chromedriver，只需要安装 Chrome；如果报错可尝试升级 Selenium。
* 运行 `main.py`，翻译结果会自动写回待翻译文件。

### 3、纠错与人工润色

* 无论使用 DeepL 还是本地 AI，完成后都建议在 Ren'Py Launcher 中运行 **检查脚本 / Check Script**。
* `postprocess.py` 可继续用于修复部分 Ren'Py 标签问题。
* 建议人工抽查人名一致性、变量、文本标签、菜单选项和 UI 文本。

## 关于接口

原项目默认使用 DeepL 网页端。本 fork 额外提供 `lmstudio.py`，可直接连接 LM Studio 或其他提供 OpenAI-compatible `/v1/chat/completions` API 的本地服务。

## 相关项目

感谢 https://github.com/libudu/renpy-deepl 项目的 JS 脚本。
