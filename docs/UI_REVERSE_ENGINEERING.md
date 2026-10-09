# 原版控制面板逆向清单

依据：工作区 `Hoiko_PPT_v0-beta1/Hoiko.exe`、反编译的 `Hoiko.MainForm`、`ConfigAI`、`PPT2Main.SetMyLife`、原版 README 和 `config.dll`。下面记录的是代码实际行为；原版文档中的“不要修改”是原始发行说明，不是本次开发要求。原始文件保留不改。

## 设置对应表

| 原版控件 / 配置键 | 还原出的行为 | Linux 面板 |
| --- | --- | --- |
| PadConnect | 插拔 SCP Xbox 手柄 | 连接手柄 / 断开；通过 uinput |
| PlayerNo / player | 存储 0–3，界面显示 1–4 | 玩家 1–4；菜单状态目前验证双人布局 |
| ViewNext / viewNext | 2、3、4、5、12；PC stack 要求 12 | 2–5 个可见 Next；附加当前方块传入求解器 |
| Speed / speed | 仅允许 25、33、50、100；前端按帧节流 | 1–100%，滑条和键盘输入；运行中生效 |
| BeamSize / beamSize | 按钮步进 50；原版动态调整宽度 | 1–500 固定搜索宽度 |
| UseHold / useHold | 禁用会关闭 TD、DPC | 已接通，并联动 TD 和 TD loop |
| UseTDOpeners / useTDOpeners | 有效条件：Next ≥ 5 且 Hold | 已接通恢复的模板算法 |
| AutoCharSelect / autoSelect | 角色选择自动输入 | 已接通当前版本三层菜单，读取状态确认 |
| UseOffsetOff / useOffsetOff | 相杀外し：根据对手攻击、垃圾和时间选择反击窗口 | 尚未移植；列在面板清单中，不提供无效开关 |
| UsePCstack / usePCstack | 全清导向堆叠；Next ≤ 5 时界面取消勾选 | 需要尚未适配的 12 Next，暂不可用 |
| UseS4W / useS4W | 侧边四列井评估，并结合对手威胁 | 已接通恢复的评分开关；对手威胁观测未移植，不能声称与原版完全一致 |
| LoopTemplate / loopTemplate | 只有 TD 或 DPC 有效时才能启用 | TD loop 已接通，并校验依赖 |
| ThreadCount / threadCount | 夹在 1–2 | 当前串行核心固定 1，面板说明 |
| Lifespan / lifespan | 1/3/5/10/30/60/1440 分钟；1440 显示 inf；每局重新设置截止帧 | 不限/1/3/5/10/30/60；每局按墙钟计时（与原版按全局帧有细微差别） |
| PlayStyle / playStyle | 控件隐藏；setter 存储，但 getter **固定返回 VS** | 记录在清单，实机只接入本地 VS |
| MinDepth / minDepth | 控件隐藏；getter **固定返回 10** | 独立提供 Linux 实际搜索深度 1–14，不假装是原版生效参数 |
| TakeOverTime / takeOverTime | 控件隐藏；getter **固定返回 5.0** | 记录在清单；另提供 Linux 搜索软预算 ms |
| UseDPC | 控件隐藏；勾选处理立即设置为 false | 原版也不可用，列在清单 |
| Option1/2/3 Radio | 切换基本/策略/高级三个选项面板 | 基本设置 / 搜索与运行 / 原版设置清单 |
| SearchResultLanguage | 日文/英文切换四个结果标签 | 中文统计（深度、耗时、节点、完成落块）；不把节点数冒充原版 ren/over |
| SearchResult | depth、beam、ren、over | 展示现有桥接层实际返回的统计 |
| TetrizVisualize | 主窗口加载时隐藏；显示内置模拟器面板 | 按用户要求不提供棋盘预览；独立模拟器未移植 |
| TetrizStart / TetrizAutoPlay | 内置模拟器开始/结束、自动玩；不是外部游戏的开关 | 不混同为原版实机设置；另提供实机开始/暂停 |
| Pad ↑↓←→ A B Y | MouseDown 手动输入，MouseUp 释放 | 暂停自动后，单次短按并自动释放 |
| FormClosed | 停止线程、拔手柄、写 config.dll | 停止子进程、释放输入；独立保存用户设置 |

关键定位：`MainForm_Load` 25410 行附近；设置事件 25530–25870；隐藏控件在 25490–25510 附近；`ConfigAI` getter/setter 在 7970–8195；原版速度节流在 `PPT2AI.Main` 3022 行附近。

## 新速度定义

100% 保持 Linux 后端已有约 50 ms / 操作的节奏。5% 的最小按键起始间隔为 1000 ms，10% 为 500 ms。它是相对于 Linux 后端的输入频率比例，不保证真实落块数严格成比例，也不声称与 Windows 原版的 60 Hz 调度完全相同。

每个移动/旋转仍采用短按、读取游戏回执、释放；额外等待只发生在释放后，所以低速不会触发长按连移。游戏重力、消行动画、计算时间与等待回执仍按游戏原速进行。等待期间若方块变化会丢弃旧动作，下一轮重新求解。停止请求可打断等待；暂停自动在当前方块指令完成后生效并保留手柄。用户暂停游戏时，对局时钟停滞会停止自动输入。

## 设置与进程

- `./hoiko gui` 或 `./hoiko` 打开 GTK4 面板。
- 使用系统 Python 的 PyGObject、GTK4、pycairo；本机已具备这些依赖。
- 设置保存至 `$XDG_CONFIG_HOME/hoiko/settings.json`（默认 `~/.config/hoiko/settings.json`）。
- 原版配置导入是只读的，原版 player 索引加 1；12 Next 和未移植设置会明确报告。
- 子进程使用同一 CLI；JSON stdin 控制速度、暂停、继续、手动按键和停止，stdout 提供日志与统计。
- 界面另有只读观察线程。关闭面板/控制管道 EOF 会停止输入进程；停止会断开虚拟手柄。
- 点击面板会让游戏失去焦点。调整后点“返回游戏窗口”或“继续运行”；面板不会持续抢焦点。
