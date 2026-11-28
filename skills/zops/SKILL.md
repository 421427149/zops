---
name: zops
description: >-
  一条命令操作 ZOps 调试操作台上的开发板。当任务涉及串口、开发板、板子、UART、
  console、串口日志、看启动日志、向板子发 shell 命令、等板子输出某个特征、
  adb 到板子、查哪块板在线、弄清哪个串口是谁、给板子供电/开关机时使用。
---

# zops —— 一条命令做成一件事（开发板串口 / 控电）

ZOps 是给 AI 用的多板调试操作台：抗洪水串口终端 + 聚合网关（hub，默认
`http://127.0.0.1:8300`）+ 控电组合（rig）+ 按板名 adb。本技能是它的一条命令
AI 入口，铁律已内建——只认板名、拒绝 ttyUSBx/COMx、长轮询不 sleep 死等、
断电类操作不放行。命令本体 `{{ZOPS_CMD}}`（安装技能时按本机仓库位置自动填入），
只依赖标准库，串口读写一律经 hub（铁律 11），可用 `--hub`/`ZOPS_HUB`、
`--token`/`ZOPS_TOKEN` 指向别的网关。

## 什么时候用

| 你要做什么 | 子命令 |
|---|---|
| 向某块板发命令并等应答（看启动日志、探活、改配置……） | `run` |
| 查哪块板在线、板子叫什么、能否烧录（flash.state） | `boards` |
| 给受控板供电（仅开机；断电/重启必须人来） | `rig <组合名> on` |

## 三个例子

```bash
# 1. 发命令并等特征：命中含 "Linux" 的行，最多等 10s；--json 供机器读
{{ZOPS_CMD}} run --board "3588 A" --send "uname -a" --expect "Linux" --timeout 10 --json

# 2. 板子清单：title / hint / name_source / tab 序号 / rig / flash.state
{{ZOPS_CMD}} boards --json

# 3. 只放行 on（幂等安全）；off/coldboot/enter-loader/catch-uboot/bind 一律被拒
{{ZOPS_CMD}} rig rig-A on --json
```

`run` 的 `--json` 一行：`ok / board / tab / matched / output / elapsed_ms /
reason / capture_consistent`。板名写错时 stderr 会列出当前可用板名——
**不确定板名就先 `boards` 再 `run`**。默认跳过所发命令的回显行再匹配，
`--count-echo` 关闭。

## 退出码

- `0` 等到了 / 成功
- `1` 超时没等到（`reason: "timeout"`）
- `2` 环境或用法错误（网关不通 / 板名不认识 / 端口名被拒 / 参数错），
  原因看 `reason`：`no_such_board / port_name_rejected / hub_unreachable /
  power_action_forbidden / api_error / ...`

## 网关不通时怎么办

1. 先 `curl http://<主机>:8300/api/status` 确认网关活着（不在本机就
   `--hub http://<主机>:8300`）。
2. **不要自己起串口实例，不要直连 832x 实例端口**——绕开网关 = 绕开审计（铁律 11）。
3. 网关确实没起：如实报告给人，由人启动（`bash start_zops.sh`），不要代劳。

## 必须知道的铁律（摘录）

- **按板名（Tab title）寻址**：ttyUSBx 重插即重编号，工具会直接拒绝端口名（铁律 3）。
- **动手前先 `boards`**（铁律 1）：Tab 序号随实例上下线而变，上次记的序号作废。
- **title 是板名，hint 是证据**（铁律 2）：两者矛盾 = 接线变过，先核对再操作。
- **不要 sleep 死等**（铁律 5）：`run` 已内置长轮询，给 `--timeout` 即可。
- **断电/重启/进 Loader/救砖是真实硬件操作**（铁律 4/8）：
  `off / coldboot / enter-loader / catch-uboot / bind` 需要人来做，或任务明确
  要求后走 `POST /api/rigs/<名>/<动作>`；`on` 幂等安全可放心。
- `flash.state=offline` = 下载线没接或板子没电，**不要烧**，不是软件问题。
- **经 adb / ssh 查板子之前，先拿到该台架的 `flash.serial`，`adb -s` 只打这个序列号**
  （铁律 12）：凭"总线上就它一个"去挑设备，会把一块板的结论安到另一块板上
  （11-26 实测踩过）。不属于任何台架的设备（`unregistered`）别当某某板用。
- `boards` 读的是网关当前的登记。要**现扫**哪些板在线（插拔后的最新状态）：
  `curl "<网关>/api/rigs?fresh=1"`；在接着板子的 Linux 主机上还可用 MCP 工具
  `zops_rigs` 或 `rigctl.py online`。

## 完整契约

仓库（`{{ZOPS_DIR}}`）根目录的 `AGENTS.md`（12 条铁律 + 自检清单），与本文冲突时以它为准。
adb 操作另有 `{{ZOPS_ADBQ}} <板名> <adb子命令>`（按板名寻址，禁广播）。
