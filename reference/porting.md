# 换视频源：让流水线支持别的站点

> **先说结论**：这个工作流**不绑定 B 站或抖音**。换站点分三种情况，改动量差别很大。
> 但**没有任何保证**——各站接口、签名、验证策略都不同，有些能改通，有些改不通。
> 下面给的是**排查路径和改法**，不是一个"万能适配器"。

---

## 一、先花两分钟判断属于哪一档

按改动量从小到大：

| 档 | 特征 | 改动量 | 例子 |
|---|---|---|---|
| **A. 通用下载器已支持** | `yt-dlp --simulate <URL>` 能列出格式 | **零改动** | YouTube、Vimeo、B 站、多数公开视频站 |
| **B. 有公开接口但不被支持** | 页面上能直接看到播放地址 / 有 JSON 接口 | 写一个解析函数 | 部分国内站点、自建站 |
| **C. 需要登录 / 签名 / 反爬** | 通用下载器报 403、要验证码、要 cookie 才出流 | 走浏览器渲染 | 抖音、部分会员内容 |

**判断命令（第一档只要这一条）：**

```bash
python -m yt_dlp --simulate --no-playlist "<URL>" 2>&1 | head -30
```

- 输出里有 `format code / ext / resolution` 这些行 → **A 档，直接能跑**；
- 报 `Unsupported URL` → 往下看 B / C；
- 报 `403 / 412 / 需要 cookies` → C 档。

## 二、A 档：什么都不用改

直接喂给主流水线：

```bash
python scripts/asr_pipeline.py "https://www.youtube.com/watch?v=xxxxxxxxxxx" -o yt.txt
```

`download_media()` 里用的是 `yt-dlp` 的 `bestaudio/best`，对绝大多数站点都成立。

**两个可能需要调的地方：**

- **要登录的视频**：导出 cookie 成 Netscape 格式，用环境变量传进去：
  ```bash
  export YTDLP_COOKIES=/path/to/cookies.txt
  ```
- **播放列表**：默认 `--no-playlist` 只下第一条。要批量就把这个参数去掉、自己循环。

## 三、B 档：写一个解析函数

目标很简单：**拿到一个能直接下载的音频/视频 URL，或直接下载到本地文件**。

改 `scripts/asr_pipeline.py` 里的 `download_media()`，按站点分支：

```python
def download_media(url: str, workdir: Path) -> Path:
    host = urlparse(url).netloc.lower()

    if "youtube.com" in host or "youtu.be" in host or "bilibili.com" in host:
        return _download_with_ytdlp(url, workdir)     # A 档

    if "example.com" in host:
        return _download_example(url, workdir)        # B 档：你的解析

    raise RuntimeError(
        f"暂不支持该站点: {host}\n"
        "先跑 `python -m yt_dlp --simulate <URL>` 看是不是 A 档；"
        "不是的话参考 reference/porting.md 加一个分支。"
    )
```

B 档的解析通常长这样：

```python
import requests

def _download_example(url: str, workdir: Path) -> Path:
    """抓页面 -> 从 JSON / HTML 里取媒体直链 -> requests 下载。
    这里必须自己看目标站的返回结构，没有通用写法。"""
    html = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20).text
    m = re.search(r'"playUrl"\s*:\s*"([^"]+)"', html)
    if not m:
        raise RuntimeError("没找到媒体地址，页面结构可能变了")
    media_url = m.group(1).replace("\\u002F", "/")

    out = workdir / "src.m4a"
    with requests.get(media_url, stream=True, timeout=60,
                      headers={"Referer": url}) as r:   # 很多站要带 Referer
        r.raise_for_status()
        with open(out, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
    return out
```

**B 档的常见卡点：**

- **要 Referer / UA**：直链单独访问 403，带上来源页就好；
- **地址有时效签名**：不能缓存，每次现取；
- **HLS / DASH（.m3u8）**：别自己拼 TS 分片，交给 ffmpeg 或 yt-dlp：
  ```bash
  ffmpeg -i "https://.../index.m3u8" -vn -ac 1 -ar 16000 audio.wav
  ```

## 四、C 档：走浏览器渲染

通用下载器和我自己写的请求都会被挡时（签名、验证码、登录墙），换思路：**用真实浏览器打开页面，从渲染后的 DOM 里取东西**。

骨架：

```python
from playwright.sync_api import sync_playwright

def fetch_with_browser(url: str, proxy: str | None = None):
    with sync_playwright() as p:
        b = p.chromium.launch(headless=False)          # 有反爬的站，headless 基本必被挡
        ctx = b.new_context(storage_state="state.json",
                            proxy={"server": proxy} if proxy else None)
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)                     # 等前端渲染，别用固定长 sleep
        html = page.content()
        b.close()
        return html
```

拿到 HTML 后，还是回到 B 档的"从结构里取地址"。

**必须知道的代价：**

- 慢（每个视频几秒到几十秒）；
- 脆（改版就失效）；
- 有反爬的站会对无头浏览器弹验证，`headless=False` 才能过；
- 要维护登录态（cookie / storage state）。

**什么时候值得走 C 档**：这个站点你**经常用**，且 A/B 都行不通。偶尔用一次的话，不如让用户直接把文件发你。

## 五、加一个新站点的检查清单

1. `python -m yt_dlp --simulate <URL>` —— 能出格式就结束（A 档）。
2. 不能 → 用浏览器开发者工具的 Network 面板看：**播放时有没有一个返回媒体地址的请求**。有 → B 档。
3. 没有、或者地址带签名/加密 → C 档。
4. 先**只取一条短视频试通**，确认能拿到音频，再跑长视频。
5. 通了再考虑切段、术语纠错、时间轴这些后处理。
6. **把这次的新分支单独写成一个函数**，别散在主流程里；下次改版只改这一处。

## 六、明确不保证的事

作者只在 **B 站** 和 **抖音** 两条路上实际跑通过。其他站点：

- **能不能改通，取决于该站当时的接口和反爬强度**，不取决于这个文档写得多详细；
- 站点改版后失败是正常的，**不代表工作流设计有问题**；
- 需要付费会员、需要破解签名、需要绕过访问限制的场景，**本工作流不支持也不建议做**。

改不动的时候，正确的处理是**如实告诉用户"这个站目前拿不到"**，然后建议用户直接提供音频/视频文件或已有字幕 —— 而不是硬试、也不是假装读过了。
