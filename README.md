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
* 自动保护 Ren'Py 文本标签、变量和常见占位符，例如：
  * `{i}` / `{/i}`
  * `{b}` / `{/b}`
  * `[player_name]`
  * `%(name)s`
* 批量翻译失败时自动降级为逐条翻译，避免整个文件被错误返回污染
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

脚本会自动连接：

```text
http://127.0.0.1:1234/v1
```

并自动使用 LM Studio 当前加载的第一个模型。

也可以手动指定模型：

```bash
python lmstudio.py "D:\Games\WaifuAcademy\game\tl\schinese" --model "你的模型ID"
```

或者指定其他 OpenAI 兼容 API：

```bash
python lmstudio.py "D:\Games\WaifuAcademy\game\tl\schinese" --base-url http://127.0.0.1:1234/v1
```

#### 推荐参数

对于 7B 翻译模型，可先用默认参数：

```text
batch = 4
temperature = 0.7
top_p = 0.6
max_tokens = 2048
```

若模型 JSON 输出不稳定，可降低批量：

```bash
python lmstudio.py "D:\Games\WaifuAcademy\game\tl\schinese" --batch 1
```

若想加快翻译，可尝试：

```bash
python lmstudio.py "D:\Games\WaifuAcademy\game\tl\schinese" --batch 8
```

完整参数：

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

如果想重新翻译某个文件，可以：

1. 恢复对应 `.rpy.bak`；
2. 从 `finished_file_list_lmstudio.txt` 删除该文件路径；
3. 再运行脚本。

### 2B、原版 DeepL 网页翻译

* 在 `main.py` 的 main 函数里面输入你生成的翻译文件目录。
* 最新版本 Selenium 不再需要手动下载 chromedriver，只需要安装 Chrome；如果报错可尝试升级 Selenium。
* 运行 `main.py`，翻译结果会自动写回待翻译文件。

### 3、纠错与人工润色

* 无论使用 DeepL 还是本地 AI，完成后都建议在 Ren'Py Launcher 中运行 **检查脚本 / Check Script**。
* `postprocess.py` 可继续用于修复部分 Ren'Py 标签问题。
* 建议人工抽查：
  * 人名一致性
  * `[变量]` 是否保留
  * `{i}`、`{b}` 等标签是否成对
  * 菜单选项和 UI 文本是否被正确翻译

## 关于接口

原项目默认使用 DeepL 网页端。本 fork 额外提供 `lmstudio.py`，可直接连接 LM Studio 或其他提供 OpenAI-compatible `/v1/chat/completions` API 的本地服务。

## 相关项目

感谢 https://github.com/libudu/renpy-deepl 项目的 JS 脚本。
