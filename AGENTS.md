# AGENTS.md — ZOps 使用须知（AI 动手前读完本文件）

**ZOps（Z 操作台）= 给 AI 用的多板调试操作台**：串口观察、烧录/进 Loader、上下电。
本体是抗洪水串口终端 `serial_debug.py`；`agent_hub.py`（网关，8300）把它聚合成多板
统一入口；`rigctl.py` 管电源/救砖，`serial_keeper.py` 管实例生命周期。

细节文档在 README.md，本文件只写 AI 的行动契约；与 README 冲突时以本文件为准。

## 工具选型（按目的）

| 你要做什么 | 用什么 |
|---|---|
| 看板子串口输出 / 发命令 | MCP `zops_*` 工具（入口 `serialdev` → hub:8300）。旧名 `qserial_*` 仍作别名照收 |
| ZOps 网关不可用 / 脚本化 | `curl http://127.0.0.1:8300/api/...`（API 面 = README「AI Agent 通道」节）|
| adb 操作板子 | `python3 adbq.py <板名> <adb子命令>`；**禁 adb 广播** |
| 查谁是谁 | `curl http://127.0.0.1:8300/api/tabs`，看 title/board/hint 字段 |
| **现在哪些板在线、各是哪个台架（rig）的板** | MCP 工具 `zops_rigs`（每次调用现扫 USB 总线，只读、不动板）；脚本 `python3 rigctl.py online [--json]`；或 `GET /api/rigs?fresh=1`。`unregistered` 非空 = 有设备在线但不属于任何台架（换过板/登记没跟上）——**别猜它是谁**，认它跑 `rigctl.py <台架名> bind-dl`（会重启该板）。新会话开局会自动注入一份快照；USB 插拔时 hub 自动刷新面板并写审计。**以现查为准，不以开局快照为准** |
| 受控板电源/冷启动/进Loader | **一键接口** `POST /api/rigs/<组合名>/<on\|off\|coldboot\|enter-loader>`（人网页按钮走的就是它；动作异步执行，结果落在 `GET /api/rigs` 的 `last` 字段）；或命令行 `python3 rigctl.py <组合名> <同款动作>` |
| **要刷机 / 需要知道"刷哪块板"** | 读 `GET /api/rigs` 每个 rig 的 **`flash`** 字段：`state`（`ready`=已在 Loader 可烧 / `system`=运行态，先 `enter-loader` / `offline`=下载线未接或板子没电 / `unbound`=未登记序列号）+ **`serial`**（烧录工具按它选设备）+ `download_chain`。**`flash.state=offline` 时不要烧**——那是线没接，不是软件问题；更不要退回"按类型找 2207"。面板按钮的 ✓/✗ 是**该次验证时刻**的结论，看它的 `at` 日期，✗ 的含义读 `evidence`（"未见 350b" 多半是当时下载线没接，不代表功能不存在） |
| **`/api/rigs` 里某 rig 的 `target.tab=null` / `target.usb=null`** | **不要借用别的 tab、不要自己按 adb 序列号猜板**。`tab=null` = 登记的控制台口被挪走了 → 能 adb 进该板时优先 `python3 rigctl.py <组合名> bind-console --write`（标记法，不断电）；不行再 `relocate`（电力差分，会重启该板）；`usb=null` = 该板 USB 下载线没接到主机（总线无其序列号）→ 属实，接线后再试。取串口与 USB **一律以 rig 名为准**（`rigs.json` 的 `target.adb_serial` 是差分实证的），不要手填 serial |
| 绑定 USB 下载口 / 逐动作验证 | `python3 rigctl.py <组合名> bind-dl`（断电差分+二次复核，写 `download_chain`）；`python3 rigctl.py <组合名> verify`（按钮交付前必跑，硬证据写 `verified`）；`bind-console [--write]`（标记法核对/回写控制台口，不断电） |
| **内核已 panic / 卡死要救砖** | `python3 rigctl.py <组合名> catch-uboot`（冷启动+连发 Ctrl+C 抢 U-Boot 3 秒倒计时+菜单选 `[3] USB update images` 进 Loader）。**内核死了 in-system 的 `enter-loader` 必然无效**，只有 U-Boot 这条路；`--menu` 只停在菜单不选 |
| 看组合(控制台/受控/USB)状态 | `GET /api/rigs`（控制器✔/受控✔/USB 模式一屏全览，AI 先看这个再动手）|

## 铁律

1. **动手前先 list_tabs**：Tab 序号随实例上下线而变，上次会话记的 index 作废。
2. **title 是板名，hint 是证据**：title 三级取用——`boards.json` 人工名 >
   `rigs.json` 里 `rigctl bind` 回填的槽位名（rig-A/rig-B 的 `target.name`，如「3588 A」）>
   账本签名自动派生；`name_source` 标明来源（boards.json / rig绑定 / hint自动）。
   hint 是账本尾部 `root@主机名`/SoC 签名。两者矛盾 = 接线变过，先核对再操作。
   绑定过的板子即使 boards.json 没登记/没重登记，也能按 rig 槽位显示成 A/B——
   这正是「挪线后 Tab 该叫什么」的事实源。
3. **禁止背 ttyUSBx 编号**：重插即重编号（实测一天变两次）。寻址只用板名或
   by-path 链路段（如 `1.4.2`）。
4. **`send`/`open`/`close`/`rigctl off|coldboot|bind` 都是真实设备操作**。off/coldboot/bind
   会真断电；除非任务明确要求，不要对在跑的板子主动断电重启。`rigctl on` 幂等安全。
5. **发完命令立即读，禁止 sleep 等待**：读默认 tail 模式，应答就在最新尾窗；没读全
   紧接着补读一次。等异步事件（如重启完成）就先干别的，回头查 `pending_bytes`。
6. **串口静默先查波特率再下结论**：T536 治具控制台 115200，RK 调试口 1500000，配错
   = 全程零字节。静默 ≠ 线断 ≠ 没电。
7. **实例账本疑死先做活性采样**：两次 `/api/info` 采 `capture_bytes`，不涨 = 大概率
   设备重插后旧实例持死 fd，重启实例（用 by-path）而不是对着冻死账本下结论。
8. **电源类操作必须以"回读"为准，不信命令返回值**：GPIO 写完要回读
   `/sys/class/gpio/gpioN/value`，H503 要回读通道 `st`。只检查命令 rc=0 会把板子
   留在断电态（实测踩过）。任何流程中途失败，必须 `_ensure_power_on` 兜底。
9. **USB 拓扑易变**：动线/开关 H503 USB 口通道都可能让整棵子树重排（实测 1.3.x→1.1.x）。
   症状是「板子不见了/Tab 变未识别」→ 先看 by-path 现状，串口身份 keeper 会自动重建，
   下载口绑定重跑一次 `bind-dl`。绑定是快照，不是永久事实。
10. **板子身份只认两类硬证据：SoC 序列号（下载口）和电力差分（串口口）**。物理口、
    PID(2207:xxx)、主机名、声卡型号都**不是**身份——口会被挪、PID 两块板一样、主机名
    都叫 TQ3588、两块板都有 8988 声卡。拿不到硬证据时一律报"未见/无法确认"，宁可不认。
11. **串口读写一律走 hub（8300），禁止直连实例端口**：网关对每次 AI 读写做审计
   （`logs/hub_audit.jsonl`，主人用 `hub_audit_view.py` 实时围观）；直连 832x =
   绕开审计，等同私读。
12. **经 adb / ssh 核查板子之前，先用 `zops_rigs` 拿到该台架的 `flash.serial`，`adb -s` 只打这个序列号**。
    凭记忆或凭「总线上就它一个」去挑设备，会把一块板的结论安到另一块板头上（2026-11-26 实测：
    在错的板上做完环回，得出「串口线断了」的错误结论）。`unregistered` 里的设备不属于任何台架，
    别拿它当某某板用。

## 登记表（事实源；改文件即生效，hub/实例不用重启）

- `boards.json`：物理插口 → 板名映射。认出新板改一行 `name`；`note` 写证据；
  条目里的 `download_chain` 是该板 USB 下载/adb 线的物理口（bind-dl 差分实证），
  `adbq` 按名寻址靠它——控制台口和下载口**不是同一个口**，别混。
  键是**控制台适配器**所在插口；线挪了口没跟着改，网页会按 rig 绑定名兜底显示。
- `rigs.json`：控电组合。**controller = 治具常量，不要动**；target = 受控槽位，
  换板/挪线后跑 `rigctl <组合名> bind`（会真实断电再上电一次）自动重探测回填，
  旧绑定进 `_history`。target.console 同时是网页 Tab 的「A/B 槽位名」来源。

## 新板子上线 = 插线即可（serial_keeper 守护全自动）

**串口插上就有、拔掉自动收、线挪了自动跟**——`serial_keeper.py`（tmux serial-keeper 常驻）
盯 /dev/serial/by-path：新单口适配器（-port0 结尾）几秒内自动起实例（默认 1500000，
特例写 keeper.json，如 {"1.4.2":{"baud":115200}}）；消失超 120s 自动清理释放 agent 口，
回来自动重起。hub 自动发现 → 网页自动显示（hint 自动命名）。**AI 不要再手动起实例**，
仅两种情况动手：板子输出全静默想换波特率（改 keeper.json 后 kill 对应 serial-auto-* 会话
即自动重起）、要给口起正式板名（改 boards.json）。

人工/特殊实例照旧不受管：serial-t536(8324)、serial-3588(8325)、serial-compat(8321 兼容门)、
serial-audit(审计镜像)。hub 常驻检查: `curl -s http://127.0.0.1:8300/api/status`。
改 ZCode 的 MCP 配置需重启会话才生效。

**板子换过/挪过线之后**：`zops_rigs` 会把它报成 `unregistered`（或该台架离线）。顺序：
`rigctl.py <台架名> bind-dl`（电力差分，认下载口和序列号，重启该板两遍）→
`rigctl.py <台架名> bind-console --write`（标记法认控制台口，不断电）。两步做完登记自洽
（`rigctl.py --selftest` 会查同口/重复登记）。

## 自检（改码后必跑对应项再提交）

```bash
python3 check_code.py             # 语法 + 重复定义 + 拼接事故 + 内嵌JS  (改任何 py 后必跑)
python3 test_agent_http.py        # 20 项, 自包含不碰真设备(只删自己造的临时 logs 文件)
python3 test_zops_cli.py          # 一条命令入口 zops_cli.py(改它后必跑): loopback 假 hub 上验证
                                 #   run/boards/rig 的退出码与 --json 字段, 不碰真设备
python3 test_install_skill.py     # 技能安装器(改 deploy/install_skill.py 或 skills/zops 后必跑):
                                 #   装进临时目录, 核对占位符都换成本机值(命令路径、网关地址)
python3 adbq.py --selftest
python3 rigctl.py --selftest
python3 test_topology.py         # 拓扑鲁棒性回归(仿真, 不碰真设备, ~40s): 挪线/对调/拔板/换板/
                                 #   没电/没shell/电源线改接/第三方板/Loader态/安静启动/在线检测/热插拔 共27场景
python3 test_property.py 300     # 属性测试(仿真, ~40s): 随机生成接线世界, 验证5条恒成立性质
                                 #   (不串台/按序列号追踪/离线不认领); 失败会打印最小复现步骤
python3 test_mutation.py         # 变异测试(~10分钟, 提交 rigctl/adbq 的改动前跑): 把核心判断
                                 #   改坏29处看测试能否抓到; 非等价变异必须全杀, 否则说明
                                 #   "测试全过但没测到" —— 新增/修改核心逻辑时同步加变异点
python3 test_ui_smoke.py          # 改 UI_HTML 后必跑: JS语法/重复定义/翻译器行为/选中标签高亮/未登记设备行回归
python3 serial_keeper.py --status # keeper 管理视图(实例缺失会自动补, 无需手动起)
```

提交信息按天嵌规范：`(等级)[type][uart] 描述`，描述含原因+解决方式。

**自检脚本不许删 `logs/`**：`logs/hub.log`（systemd `StandardOutput=append:` 指向它）和
`logs/hub_audit.jsonl`（AI 读写审计账）是运行产物与审计证据，删了 hub 会起不来
（`status=209/STDOUT`）、审计历史不可恢复。测试若需要临时日志，只删自己新建的文件
（`test_agent_http.py` 已按「运行前快照、只删新增」实现）。

## 许可（别搞错）

本项目是**专有软件（Proprietary）· 保留所有权利**，不是开源、也不是"源码可见许可"。
不要把它描述为开源，不要建议把它分发到公开仓库、公开分享给第三方、或并入对外产品。
对外协作（如发给客户/供应商）前必须由著作权人书面授权。
第三方组件（`webui/xterm.js` 等）仍为 MIT，引用它们时保留其版权声明。
`a673302` 及更早的提交历史为 MIT，不追溯。
