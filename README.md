##两款TV端直播播放app 一款win端直播播放器软件

一款纯直播APP [https://github.com/jia070310/lemonTV](https://github.com/jia070310/lemonTV)

还有一款是影视播放器和直播APP集合功能版 [https://github.com/jia070310/lomenTV-VDS](https://github.com/jia070310/lomenTV-VDS)（影视播放器类似于网易爆米花 vidhub infuse等播放器）

一款pc端直播播放器  [https://github.com/jia070310/lemonIPTV-windows](https://github.com/jia070310/lemonIPTV-windows)

![image](https://github.com/jia070310/4K-IPTV-M3U/blob/main/tv.png)
# RTP 自动更新工具

该仓库使用 `rtp/b.py` 按省份模板自动搜集可用 `udpxy` 节点，并生成：

- `rtp/*.txt`
- `rtp/*.m3u`

## 本地运行

1. 安装依赖：
   - `pip install -r requirements.txt`
2. 设置环境变量：
   - `QUAKE_TOKEN=<你的 Quake Token>`
3. 运行：
   - `python rtp/b.py`

如需本地一键提交并推送，可使用：

- `python rtp/b.py --push`

## GitHub 定时更新（每 3 天）

工作流文件：`.github/workflows/update-rtp.yml`

- 定时表达式：`0 2 */3 * *`
- 触发方式：手动触发 + 定时触发

### 必要配置

在仓库 `Settings -> Secrets and variables -> Actions` 中新增：

- `QUAKE_TOKEN`：你的 Quake API Token

配置完成后，工作流会每 3 天自动执行脚本并提交变更到当前仓库。
