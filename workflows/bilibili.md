# B 站工作流

> 这是**一套验证过的做法**，不是唯一做法。不同 agent、不同网络、不同视频，细节可能需要调整。
> 通用流水线见 [`scripts/asr_pipeline.py`](../scripts/asr_pipeline.py)；换平台见 [reference/porting.md](../reference/porting.md)。

---

## 一、先判断：能不能不转写

**这一步能省掉大部分成本，建议每次都先查。**

1. **UP 主上传的字幕**（部分视频有 `CC` 字幕）；
2. **B 站自动生成的 AI 字幕** —— 覆盖面比想象中大，免费、即时。

查法：拿 `bvid` 与 `cid` 调播放器接口，取 `subtitle_url` 后 GET 那个 JSON：

```bash
# 先拿 cid
curl -s "https://api.bilibili.com/x/web-interface/view?bvid=BV1xxxxxxxxx" \
  | python -c "import sys,json;d=json.load(sys.stdin)['data'];print(d['cid'],d['title'],d['duration'])"

# 再取字幕列表（多数情况下需要携带登录 cookie）
curl -s -H "Cookie: $(cat cookies.txt | tr '\n' ';')" \
  "https://api.bilibili.com/x/player/wbi/v2?bvid=BV1xxxxxxxxx&cid=<CID>&aid=<AID>" \
  | python -c "import sys,json;d=json.load(sys.stdin);print(d['data'].get('subtitle',{}).get('subtitles',[]))"
```

拿到 `subtitle_url` 后直接 GET，得到逐条 `{from,to,content}` 的 JSON。

**AI 字幕的两个已知缺陷**（所以只能当参考或初稿）：

- **没有标点**，读起来是连续的字流；
- **字母缩写容易错**：`STM32` 被识别成 `s t m32`、`Cortex-M` 被识别成 `Cortex杠m`。

**交叉核对法**（很有用）：同一段内容，如果官方字幕和你的 ASR 引擎**听成同一个词**，基本可以认定是 UP 主原话，不要改；如果**只有一边是这样**，大概率是那一家的错听。

## 二、多 P 视频：不带 `p=` 只会拿到第一集

这是最常见的"处理错了还不知道"。

- 链接形如 `?p=2` 时，必须把分 P 号传给下载器，否则只会下 P1；
- 用 yt-dlp 下载时，输出文件 id 会带上 `_p2` 后缀，可以据此核对没下错；
- 分 P 标题和时长在 `view` 接口的 `data.pages[N-1]` 里（`part` / `duration`）。

```bash
# 只要 P2 的音频
yt-dlp -f bestaudio --no-playlist \
  "https://www.bilibili.com/video/BV1xxxxxxxxx?p=2" -o "p2.%(ext)s"
```

## 三、412 Precondition Failed

部分 B 站视频在未登录状态下网页请求会被拒（412），表现为"下载器直接报 412"或"能拿到页面但取不到流"。

处理顺序建议：

1. 先确认不是链接本身失效；
2. 导出浏览器里已登录的 cookie 成 Netscape 格式，用 `--cookies` 传进去；
3. cookie 里必须有 `SESSDATA`，否则只算游客。

```bash
export YTDLP_COOKIES=/path/to/cookies.txt
python scripts/asr_pipeline.py "https://www.bilibili.com/video/BV1xxxxxxxxx"
```

⚠️ cookie 等于账号凭据。**不要提交进仓库、不要贴进对话、不要写进笔记。**

## 四、长视频切段

单段太长会遇到服务端负载限制或超时。经验值：

| 参数 | 取值 | 说明 |
|---|---|---|
| 单段时长 | 240 秒 | 16k/mono/16bit wav，base64 后约 9.8 MB |
| 段间冷却 | 15 秒 | 避免触发限流；49 分钟视频约 13 段，总耗时约 8~9 分钟 |

**一个容易误判的点**：240 秒音频的 base64 是 **9.8 MB 左右**，不是 3~4 MB。有人按 3.4 MB 估算后以为"负载太大肯定不行"，其实是算错了。多数服务端吃得住。

## 五、钱怎么省

**关掉思考（thinking）**。如果用的是支持推理开关的音频模型，转写任务默认会先"想一遍"，实测多花约 40% 的 token，而文本几乎没区别。

```json
{"thinking": {"type": "disabled"}}
```

各家参数名不同（有的叫 `reasoning_effort`），不支持就删掉这一行。

**模型选经济档**。同一家的音频模型通常分几档，最经济的档位做纯转写够用；质量不满足再往上换。具体型号和价格变化很快，以服务商当时的官方页为准。

## 六、术语纠错：技术类视频必做

**这是 ASR 最稳定的失效点，也是最花时间的地方。**

模型对**英文缩写、代码标识符、软件名**错得极有规律——同一种错法会在整篇反复出现。典型形态：

| 转写结果 | 实际 | 错在 |
|---|---|---|
| `STM三二` / `STM 三二` | `STM32` | 数字被读成汉字 |
| `Q5 MDK` / `K0` / `K5` / `KO` | `Keil` | 品牌名被听成同音字母 |
| `GPIO Pin` / `GPIO mode` | `GPIO_Pin` / `GPIO_Mode` | **下划线被听成空格** |
| `GPIOPin12` | `GPIO_Pin_12` | 下划线丢失 |
| `#if !define` | `#ifndef` | 单词被劈开 |
| `HAL_GPIO_ReadPin(...)` | `GPIO_ReadInputDataBit(...)` | **模型按自己熟悉的库补全**，不是听错 |
| `GPIO_Init`（讲"反初始化"处） | `GPIO_DeInit` | 前缀被吞，语义反转 |

**三条实践建议：**

1. **按"错法"写规则，不按单词写**。`下划线→空格`、`连字符→"杠"`、`数字→汉字` 这几类正则能覆盖大部分。
2. **语义反转的必须人工核**。`Init` / `DeInit`、`高/低`、`开/关` 这类被听错的，程序改不出来，只能看上下文。
3. **模型会"自作聪明"补全**。同一段里出现不属于该项目的库前缀（比如讲标准库却冒出 `HAL_`），基本可以判定是模型补的，不是原话。

**模型还会加括号注解**：给术语自动补全称，写成 `GPIO（通用输入输出）`、`VDD（电源电压）`。
判据：**同一个术语在全篇反复被加注 = 模型加注**（真人只在第一次介绍时说全称）。
删注解时注意会产生**汉字-拉丁粘连**（`接 VDD（电源）3.3V` → `VDD3.3V`），删完要再校一遍。

## 七、跨段接缝

切段处的两个固定毛病：

- **断句断开**：`…它。` + 新段 `是用来读取…` → 应接成 `…它，是用来读取…`
- **幻觉词**：段边界偶尔吐 2~4 个无意义的字（`续部分。`、`拉烂。`），删掉即可

如果上游给的是**带时间戳**的分段结果，接缝问题会少很多；只给整段纯文本的话，接缝只能靠读上下文判断。
