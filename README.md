# HRAgent 企业人才库 · 项目说明

> 本文件说明项目**现状**。历史踩坑与产品决策在 `.workbuddy/memory/MEMORY.md`。

## 一、位置与版本

| 项 | 值 |
| --- | --- |
| 工作区 | `E:\WorkBuddy\HRAgent_v2`（2026-10-22 从 C 盘迁来） |
| 当前版本 | **v2.2.5**（已打包，2026-10-22 端到端验证通过） |
| 技术栈 | FastAPI + uvicorn + 原生 JS 单页 + PyInstaller onedir |
| 数据目录 | `%LOCALAPPDATA%\HRAgentData`（**与安装目录解耦**） |

旧目录 `E:\WorkBuddy\HRAgent`（27G）是 10-09 之前的 v1.x 快照，已弃用但未删。
C 盘原目录 `C:\Users\admin\WorkBuddy\HRAgent`（4.8G）保留作回退，暂未清理。

## 二、目录职责

| 目录 | 内容 | 备注 |
| --- | --- | --- |
| `repo_src/` | **真正的源码**（3297 个 .py） | 打包入口在此，`webapp_launcher.py` 在其根目录 |
| `installer/` | 安装程序（5 个脚本） | 见下节 |
| `_package_cfg/` | 打包专用配置（模型/档位/邮箱） | **不能用 `load_cfg()` 替代**，向量与收件人模型要对得上 |
| `tools/` | 打包与验证脚本 | 注意有两个安装器源码，别混用 |
| `HRAgent工作台/` | 旧打包产物 639M | 可删；`Add-Type`/COM 被安全策略拦，暂留 |
| `_demo_resumes/` | 演示简历 | |
| `icons_candidates/` | 图标候选 4.8M | |
| `_memory_archive_logs/` | 早期记忆归档 | 默认不读 |

`repo_src` 内的 `.runtime_deps` / `.runtime_local` 是**两套重复的运行时**
（各 4382 个 .py），迁移时已排除。若需要本地跑源码，再从别处拷一份。

## 三、安装程序（installer/）

**目标：让 HR 双击一个 exe，装到自己选的路径。**

架构前提：数据落`%LOCALAPPDATA%\HRAgentData`，与安装目录无关，
所以代码可以装到任意目录（含只读位置），**这正是能做成安装包的前提**。

| 文件 | 作用 |
| --- | --- |
| `setup_wizard.py` | 安装向导本体（tkinter）：选路径 → 关运行中实例 → 解压 → 建快捷方式 + 卸载项 |
| `setup_boot.py` | 打包后的入口：把自己复制到 `%TEMP%` 再跑向导 |
| `make_setup.py` | 构建：压载荷 → 以 `datas` 嵌进 exe → **产出单文件** |
| `HRAgent.iss` | Inno Setup 脚本（备用方案，见下） |
| `build_installer.py` | 调 ISCC 编译 .iss（备用方案） |

构建：
```bash
cd E:\WorkBuddy\HRAgent_v2
python installer/make_setup.py
# 前置：先跑 PyInstaller 生成 dist\HRAgent工作台\
```

**为什么自己写而不用 Inno Setup**：本机装不到ISCC——
官网下载已迁 GitHub releases（代理拦 GitHub），`files.jrsoftware.org`
老路径全404，winget 也拉不动。安装器只用标准库，零外部依赖。

## 三·五、热更新（tools/hra_updater.py）

**状态：源码曾被清空成 0 字节（10-21 18:59，另一个 agent 操作导致），已从
`__pycache__` 的 pyc 完整重建并验证。**

设计（详见 `docs/打包与热更新方案.md`）：

| 能力 | 实现 |
| --- | --- |
| 更新范围 | 只换安装目录里的程序文件（exe + `_internal/`），**结构上碰不到用户数据** |
| 路径白名单 | 更新包里出现用户数据目录 / data 段 / 库文件 → **报错拒绝**（不是静默跳过） |
| sha256 校验 | 先验后写，包坏在写之前就被拦下 |
| 写前备份 | `_update_backup/<时间戳>/`，写入中途失败自动回滚 |
| 数据快照比对 | 更新前后各给用户数据目录做快照，不一致 → 回滚并报出具体变动文件 |
| 自我替换 | 更新前把更新器自己搬到 `%TEMP%`（Windows 锁住运行中的 exe） |

已通过端到端验证（2026-10-22，对着真实成品跑）：
正常更新（用户数据 174 文件**独立复核**零改动）/ 越界路径拦截 /
sha256 不符拦截 / 库文件拦截 / 路径深度判据 /
**闸门边界 17 项**（`tools/_test_updater_gates.py`，既钉死该放行的也钉死该拒的）。

### 更新器的三个坑（都实测踩过）
1. **路径闸门太宽会误杀合法更新**：`.secret`/`.db` 曾被列进全局禁后缀，
   把 `_internal/config/imap.secret`（配置模板）和 `_internal/seed/data/workbench.db`
   （演示种子，都是**程序资源**）当用户数据拒掉 → 更新链直接断。
   现在分层：用户数据靠 `is_user_data_path()`；seed 豁免**只放 `.db/.sqlite*` + 简历原件**
   （整目录放行会漏掉 `_internal/seed/master.key`）。
2. **搬走自己还不够，必须让原进程退出**：`_relocate_self` 用 `subprocess.run`
   同步等待 → 父进程一直活着、仍打开原 exe → 替换自己必然 `WinError 5`。
   改成 `Popen` + 立即返回。
3. **卸载自清要三级接力**：Windows 锁住运行中的 exe，
   `rmtree(ignore_errors=True)` 会**静默失败**（用户看到"卸载完了目录还在"）。
   路径必须走**环境变量**（不能进 .cmd / cmd 命令行——中文路径按 OEM 码页变 `????`）。

⚠️ `tools/hra_updater.py` 与 `repo_src/update_agent.py` 是**两套并存**的更新机制，
前者是给用户用的 GUI 工具，后者是程序内调用。改动时想清楚改哪套。

## 四、待办

### 打包（已完成 2026-10-22）
- [x] 用 `envs/hragent` 打主程序 exe（`envs/pack` 缺 uvicorn/PIL，打出来 30MB 装上就崩）
- [x] 安装器/更新器 exe（用 `envs/pack` 打，带 tkinter）
- [x] 端到端验证：安装 → 端口 8756 → 热更新 → 数据零变化 → 卸载
- [x] 交付物一致性验 md5：三个 zip 里的 exe 同一份（`9da40994…`），且与实测通过的那份一致

**交付物**（`release/`，各 168.6 MB）：
| 文件 | 用途 |
| --- | --- |
| `HRAgent程序包-v2.2.5.zip` | 给安装器用（可选安装路径） |
| `HRAgent更新包-old-to-v2.2.5.zip` | 热更新（428 个变更文件） |
| `HRAgent工作台-Windows版-v2.2.5.zip` | 免安装版 |
| `release/HRAgent工作台/` | 已组装好的发布目录（安装器+更新器+说明） |

### 打包前审计（2026-10-22 已过一遍）
| 项 | 结果 |
| --- | --- |
| 0 字节文件 | 无 |
| 语法检查 | 299/299 ✓ |
| app 模块导入 | 27/27 ✓ |
| tools 四模块导入 | 全 ✓ |
| spec 依赖路径 | 全 ✓ |
| 端口修复实测 | `_port_usable(8756)`=可用 ✓ |

**审计中发现并已修的两个问题**（都是"看代码看不出来"的）：
1. **`_pack_data` 缺失** —— spec 引用它且缺了就 `raise SystemExit` 直接中止打包。
   迁移时误当临时产物排除了，已补回。
2. **版本号停在 2.2.4** —— 代码已是 v2.2.5 的修复，但 `version.py` 没改，
   打包会标成 2.2.4。已改为 2.2.5。

### 已知问题
- [ ] E 盘旧副本含**明文 `.github_token`**，建议清理或改用环境变量

### 凭据策略（2026-10-22 用户决定：清空、不随包分发）
交付包**不含**任何密钥：模型 API Key（`secrets.json.api_key` 置空）、
邮箱授权码（删 `imap.secret`）、专属 MaaS 端点（`base_url` 换中性占位）。
真值备份在 `_vault/`（白名单外，不进包）。

`HRAgentWebapp.spec` 有**硬守卫**（命中即 `raise SystemExit` 中止打包，
不是打一行提醒）：扫 `datas` 命中 `sk-ws-…` / `ws-xxxx.` / 专属端点即中止；
`_package_cfg/imap.secret` 存在且非空即中止。
**新增凭据字段时要把新模式加进 `_CRED_PATTERNS`。**

代价：收件人首次使用需在「系统配置」自己填 API Key 与邮箱授权码。

### 清理（不急）
- [ ] C 盘 4.8G 原目录，确认 E 盘跑通后删除
- [ ] `HRAgent工作台/` 639M 旧产物
- [ ] E 盘 `HRAgent` 27G 旧快照

## 五、v2.3.0 要点（前端：弹窗与输入框整理）

### 5.0 交付包**不带任何数据**（2026-10-26 业务方要求）

只更新程序与前端，**数据与配置一律用用户原本的**：

| 场景 | 结果 |
|---|---|
| 全新安装 | **空库**（people=0），HR 自己导入简历 |
| 老用户升级（安装器） | 迁移**他自己的旧库**，旧目录原样保留 |
| 已有档案的机器再装一次 | 检测到库已存在 → **跳过迁移**，不覆盖 |
| 用户已有的配置 | `seed_config` 只补缺失项，**不覆盖**已有值 |

实现：`HRAgentWebapp.spec` 里删掉了 `_pack_data → _internal/seed` 的整段注入。
包内现在**没有** `workbench.db`、没有 `seed/`、没有 `archive/` 简历原件。
配置模板（`interview.json` / `stages.json` / `tiers.json` 等 15 个文件）仍然保留 ——
否则界面下拉框会全空。

**为什么必须去掉**：启动器会把 `_internal/seed` 当成一个"旧版数据根"自动迁移，
于是任何人第一次打开都先看到 **10 个虚构演示人**。这有两个坏处：
分不清是自己的数据还是假的；更糟的是**迁移失败看起来像成功了**（库里确实有人，只是不是他的）。

⚠️ 从旧版（≤v2.2.5）升级的用户，`_internal/seed/` 会残留在程序目录里（更新器只增改不删，
避免误删数据）。**它不会再被迁移**（用户目录已有档案时直接跳过），确认无误后可手动删除。

### 5.1 前端改动

方向由业务方定死：**不用强对比颜色、不用框线**。所以这轮改动本质是"减噪"，
不是重做布局 —— 视觉上变安静，不会变得"焕然一新"。

**输入框**（`ui_v2.py` 的 `.cinput` / `.crow` / `.dlg-input`）：
`border:1px solid var(--line-2)` + 白底 → **透明边框 + 浅灰填充底**。
`--line-2`(#c9cdd4) 本来是全站**强分隔线**，被拿去画输入框，密集表单里线互相打架。
focus 从"2px 饱和主蓝实线"改为"底色转白 + 极浅蓝描边 + 3px 柔光"。
四态齐：hover / focus / disabled / `.is-bad`。新增令牌 `--field/--field-hover/--field-line/--scrim`，
**没动任何既有配色值**（业务方 2026-10-20 定过"整个系统配色用原本那套"）。

**弹窗**（`#mask` / `#dlg`）：
- 三段式（标题 `flex:none` / 正文唯一滚动 / 底栏 `flex:none`）—— 原来容器没有 max-height，
  26 行失败明细会把弹窗撑出屏幕，**底部按钮掉到看不见的地方，且整页滚不动**（`body` 是 `overflow:hidden`）。
- 尺寸分档 400/520/720/940（原来只有 520 与 `.wide` 940 两档）。
- 遮罩 `.36` 纯压暗 → `.28` + `blur(2px)`；容器单层重阴影 → 双层，圆角 10→12。
- 窄屏 <600px 降级底部面板；动画只动 opacity/transform；`prefers-reduced-motion` 全关。
- **无障碍**（原来 `role`/`aria-modal` 有，其余全缺）：补 `aria-labelledby` + `tabindex=-1`、
  Esc 关闭、**Tab 焦点陷阱**、**关闭后焦点归还**、门控锁（进度条与危险态禁 Esc/点遮罩）。
- 12 处各写一遍的 `classList.add('on')` 收敛成一个 `openDlg(opts)` 入口。

**弹窗减法**：纯成功 / 空态 / 字段级校验降级为 `toast`（无文件可上传、上传全部成功、
批量重跑完成、无需重跑的人）。弹窗只留四类：有失败明细、导入报告、部分失败、进度门控。

**白屏守卫缺口已补**：`check_ui_js.py` 原来只 `from app import ui`（只验 `/v1`），
改主界面 `ui_v2.py` 时它全绿 = 没有任何守卫。现在遍历两套渲染函数 + 取页面里所有 `<script>` 块。

**验收**：`tools/_ui_v2_check.js` 28/28（真实 Edge + CDP）、`tools/_ui_modal_check.js` 23/23；
回退验证有区分力（去掉 aria/Esc/焦点/overflow → 6 项 FAIL；只回退 `.cinput` 边框 → 2 项 FAIL）。
设计规范与踩坑 10 条：**[docs/前端弹窗与输入框优化方案.md](docs/前端弹窗与输入框优化方案.md)**

## 五点五、v2.2.5 要点（端口判定）

用户症状是"端口冲突、地址每次都变、书签失效"。根因不是端口被占，
而是**判据错了**：

装了安全软件的机器上，`connect` 探测对**完全空闲**的端口也不返回
`10061ECONNREFUSED`，而是拖到超时回 `10035 WSAEWOULDBLOCK`。
旧代码把 10035 当"有人监听"→ 110 个候选端口全被跳过。

**铁律：判"端口空不空"只认 `bind`，`connect` 只能用来写提示文案。**
已改 4 处：主循环不再被 `_port_busy` 短路、`_port_usable` 去掉可疑确认、
旧实例探测的退出条件、提示文案（避免把"被程序占用"误报成"被系统保留"）。

## 六、2.0.0 老用户升级（v2.2.5 补的三个断点）

用户症状：**「打开在线简历」和「文件路径」总是提示路径不对**。
不是单一 bug，是**三个断点叠在一起**：

1. **`layout()` 不认 2.0.0 的 `_internal` 布局** —— 2.0.0 是解压即用版，
   库在 `HRAgent工作台/_internal/data/workbench.db`。安装器让用户手选**外层**
   目录时，旧 `layout()` 只认 `<root>/data` → 判定"这里没有数据" → 不迁移 →
   HR 打开就是空库（看着像数据丢了）。
2. **配置里的绝对路径没人搬家** —— 迁移只改写了**数据库**里的路径
   （`rewrite_db_paths`），`mailbox.json` 的 `eml_dir` / `folder_dir` /
   `archive_dir` 原样复制。2.0.0 的用户把来源目录设成了绝对路径 →
   升级后仍指向旧安装目录 → 来源目录"不存在"、在线预览全 404。
3. **安装器的候选目录被同名误伤** —— 2.0.0 的解压目录就叫 `HRAgent工作台`，
   而排除条件写的是 `name != APP_NAME` → 把真正的旧安装排除掉了。

已修（新增 `normalize_config_paths()`，`migrate_profile` 与启动流程都调）：

- `layout()` 增加 `_internal/data` + `_internal/config` 候选
- `normalize_config_paths()`：配置里的目录指向旧位置时改到当前数据根下，
  **用户自己指定的外接盘目录原样保留**；改前自动备份
- 安装器：库的位置一律走 `layout()` 判定（不再硬写 `<g>/data/workbench.db`）；
  排除条件改成**绝对路径**比较；候选目录扩到桌面/文档/下载/上两级
- 404 文案说清缺什么：区分"来源目录不存在"与"文件被移走"

**实测**（`repo_src/tests/`）：

| 测试 | 结果 | 覆盖 |
|---|---|---|
| `test_200_upgrade.py` | 14/14 | layout 识别、配置重写、外接盘不动、旧库补列 |
| `test_installer_legacy_detect.py` | 8/8 | 自动发现、手选、干净机不误报、端到端迁移 |
| `test_200_e2e.py` | 11/11 | 2.0.0 目录 → 检测 → 迁移 → **子进程实测在线预览取得到文件** |
| `e2e_200_built.py` | 18/18 | **成品级**：真实 exe 启动 → 登录 → 查候选人 → **在线预览取到原件**（跑真实打包产物） |

判据**两边都验**：回退修复后 6 项 / 5 项失败，恢复后全过（恒真判据只给假安心）。
成品级测试也回退验证过：源码回退 → 源码级 3 项 FAIL、成品级 1 项 FAIL。

**打包后已验证新包确实带修复**（不靠打包成功推断）：
主程序 exe 的 CArchive 入口 + PYZ 内 `app.profile_migrate` 都含
`normalize_config_paths` / `_is_under`；安装器 exe 的 PYZ 内
`hra_profile_migrate` 同样含；三个交付 zip 里的 exe md5 完全一致。

升级操作指南：**[docs/升级指南-2.0.0到v2.2.5.md](docs/升级指南-2.0.0到v2.2.5.md)**
