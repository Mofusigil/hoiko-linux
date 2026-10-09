# Hoiko Linux 恢复记录

## 原始程序

`Hoiko_PPT_v0-beta1/Hoiko.exe` 是 PE64 C++/CLI 混合程序集。算法主体以 CIL 保存在程序集内；WinForms、Windows 进程读取和 SCP 虚拟 Xbox 驱动属于平台层。`we.dll` 等权重文件实际是 CSV 数据，不是动态库。

使用 ILSpy 和 dnfile/dncil 读取程序集，恢复搜索、评估、方块移动/踢墙、模板和棋盘方法。`tools/recover_core.py` 沿调用关系生成 173 个方法的 C++，保留原始分支、内存字段布局及静态表；方法 token、字段名、源 SHA256 和替代方法见 `native/generated/manifest.json`。

这些生成函数在 Linux 原生执行，不启动 Windows EXE 或 CLR。`native/cil_runtime.hpp` 实现所需 CIL 数值/内存语义；`native/bridge.cpp` 提供 `hoiko_init`、`hoiko_solve` 和 `hoiko_error`。

## 平台替代

替代了权重读取、原生缓冲区分配、线程启动、STL 排序/分区、计时和原子操作等九个入口。评估串行执行，排序使用本机 `std::partial_sort`；同分项的排序可能与原版 MSVC 不同。

原版前端维护两个预测根节点；便携 API 接收实际棋盘、当前方块、可见 Next 和 Hold，在每次搜索前同步可见前缀。60 步模拟覆盖空 Hold 的双块消耗、交换 Hold 的单块消耗、坐标合法性和消行结果。

公开参考：[ultimacrown/HoikoCode20230120](https://github.com/ultimacrown/HoikoCode20230120)。该仓库提供另一版原作者算法代码，本移植生成核心来自工作区中的 `Hoiko.exe`。

## 当前 PPT2 版本

Steam build：15220611；EXE SHA256：

```text
02b556b57be7c2cc77a8b76a7c4b0ae64ef762fb55c9e2ab62e327c658cdbb86
```

原版的固定全局地址不能用一个统一差值迁移。当前配置逐项给出 RVA，运行时加上 `/proc/PID/maps` 中的模块基址：

| 字段 | 当前 RVA | 验证依据 |
| --- | --- | --- |
| 对局对象指针 | `0x1d9edf8` | 双方玩家、棋盘、当前方块和 Next 链 |
| 帧计数 | `0x1eec5bc` | 约 60 Hz 增长 |
| 模式对象指针 | `0x789610` | 指向对象 +488 是 VS 模式 14 |
| 玩家数量 | `0x1d3d0d0` | 本地双人 VS 为 2 |
| 角色 UI 根指针 | `0x1edb0b0` | 2P 光标从 5 → 6 → 7 随输入变化 |
| NetworkManager 指针 | `0x1db06f8` | RTTI 与离线 session=0 |

玩家主对象仍沿原版链读取：`match + (player+22)*24 → +136`。主对象 +7352 是棋盘，+7360 是当前方块，+7368 是 Hold。棋盘包含 10 个列指针，各列 32 个整数；非负格子视为占用，负值消行动画阶段会等待。

Next 位于玩家容器 +152 指针对象的 +360，**每项 8 字节**。这与原版 `PPT2AI.GetPPTNext` 的 `num2 += 8` 一致。该数组是未来五块；便携求解器另外读取当前方块并将其放在队列首部。

当前版本使用模式 14、无活动网络 session 和玩家主对象 RTTI `TetrisFieldClassic@Field@Tetris` 联合识别允许接入的本地 Tetris VS。不会用错误的旧 flag 地址作为有效状态。

## 菜单与输入

原版 `PPT2PreMain.SelectCharacter` 根据角色索引导航，目标索引 7 时发送 Y+A，持续执行到对局开始。旧 Linux 前端的 Down、A 固定流程不能处理操作方式确认。2026-10-04 改为读取 UICharacterSelectPlayer 的状态字节，逐层确认。角色指针消失或对局对象出现后停止菜单输入。虚拟手柄保持连接，进入对局后直接用于操作。

Linux 使用 `uinput` 创建 USB Xbox 360 特征的手柄，SDL 确认 `gamecontroller=1`，Steam/Proton 中实机确认控制 2P。窗口激活通过 XWayland/EWMH，只定位属于游戏 PID 且标题匹配的窗口。

操作码来自原版：l/r 左右，a/b 顺/逆时针，f/h 空 Hold/交换 Hold，u 硬降，d/g 加一个高度字符表示软降。移动、旋转、Hold 和落块均等待内存回执。换块的短暂空指针会等待；每次按键有最短持续时间、超时和释放处理。

## 验证结果

2026-10-03，本机 Steam/Proton 实测 2P 连续完成 40 次落块；日志包含空 Hold、交换 Hold、旋转、软降和硬降。一局结束时 1P 状态为 12，2P 为 8，2P 获胜。

离线核心四项测试通过；其中七袋模拟连续验证 60 步，独立根据形状和棋盘检查碰撞、方块格数和消行。四项测试也在 AddressSanitizer/UndefinedBehaviorSanitizer 构建下通过。

未验证：PPT1 实机、新版游戏之外的 EXE、键盘默认键位、TD 开局的实机行为，以及与原版 Windows 程序逐步选择的一致性。

## 重新生成核心

普通构建直接使用 `native/generated/`，无需原版 EXE。以下可选恢复流程需要自行准备对应版本的 `Hoiko.exe` 并放到 `Hoiko_PPT_v0-beta1/`；原版 EXE、驱动 DLL 和 `reverse/` 中的本地分析材料不纳入仓库。

```sh
python3 -m venv .tools/python
.tools/python/bin/pip install -r tools/requirements-reverse.txt
.tools/python/bin/python tools/recover_core.py Hoiko_PPT_v0-beta1/Hoiko.exe
./build.sh
```

Sanitizer 构建：

```sh
cmake -S . -B build-asan -DCMAKE_BUILD_TYPE=RelWithDebInfo -DHOIKO_SANITIZE=ON
cmake --build build-asan -j2
HOIKO_TEST_LIBRARY="$PWD/build-asan/libhoiko_core.so" \
LD_PRELOAD=/usr/lib/libasan.so ASAN_OPTIONS=detect_leaks=0 \
python3 -m unittest discover -s tests -v
```

不同发行版的 libasan 路径可能不同。正常 Release 构建不需要 libasan。

## 新版菜单状态补充（2026-10-04）

`character_root → +304` 得到场景的 UI 指针集合；`+104/+112` 分别指向 1P/2P 的 `UICharacterSelectPlayer`。

- 对象 `+704` **单字节**标记：0 角色、1 玩法、3 操作方式、7 已就绪。
- 对象 `+589` 的 `0x40` 位：Tetris；清零为 Puyo。
- 对象 `+683`：控制预设，0 普通 A，1 普通 B。

不要用 int32 读取 +704：邻接填充含旧堆地址，实测会出现负数。角色原有坐标链保留。自动选择按角色、左右选择玩法、恢复普通 A、确认就绪依次处理；只发出按键不再视为确认成功。丢失按键会重试，持续无回执时有次数上限。当前仅为已验证的双人布局提供菜单字段。

本次实机验证从角色索引 6 导航至 7，通过三层菜单进入游戏，完成 70 次落块，包含空 Hold、交换 Hold、旋转、软降与硬降。后续游戏暂停时发现旧实现仍尝试输入；新增对局时钟监测用于停止暂停时的自动输入。

## TD 模板的负行候选修复

控制面板策略测试触发 TD 搜索路径中的偶发崩溃。ASan 定位到 `ExpandAI.DeleteSameNode`；诊断确认候选的落点底行等于 -1，被直接用于 20 行去重桶的索引。原版 `Board.LockMino` 只检查顶部超界，没有检查底部超界。

Linux 恢复代码在 `Board.LockMino` 入口新增实际四格位置验证：拒绝落在棋盘外、水平截断或与现有格重叠的候选，沿原有 -1 返回值丢弃候选。有效落点继续执行原始算法。生成器也包含此校验，重新生成不会丢失修复。新增直接构造 y=0 横 I（实际占据 -1 行）的回归测试，并检查棋盘内存不被改变。
