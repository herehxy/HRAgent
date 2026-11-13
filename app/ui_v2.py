"""人才库 v2 —— **独立预览版**（不覆盖原 `ui.py` 的 `_PAGE`）。

设计取向（v2，从逻辑层重做，不沿用旧视觉）：

    这个工具只做三件事：**找人 → 判断合不合适 → 推进他**。
    旧版把三件事平铺在一条很长的卡片流上：每张卡片 250~450px 高、上面压着 6 行筛选控件，
    1080p 一屏看不到一张完整卡片。v2 按"任务的动线"重排：

        ┌──────────────────────────────────────────────────────┐
        │ 人才库  [搜索…]  学历▾ 阶段▾ 档位▾        26 人       │ 52px
        ├───────────────┬──────────────────────────────────────┤
        │ ● 苗雨桐  B   │  苗雨桐  硕士·西安电子科技大学 211   │
        │   硕士·西电   │  档位 [A] [B] [C]    [推进初筛通过]  │
        │ ───────────── │  技能命中 / 缺失（带原文证据）        │
        │ ● 祁亦寒  A   │  系统分析                            │
        │   …           │  操作                                │
        └───────────────┴──────────────────────────────────────┘

    三条设计原则：
    1. **一屏内完成三步**，不在页与页之间跳（左选右看，右直接推进）。
    2. **证据默认可见**，不折叠 —— HR 做取舍看的就是"凭什么是这个人"。
    3. **单一强调色**（墨青）只标"当前选中 / 可点"，其余靠分隔线与字号分层。

⚠️ 这是**预览版**：只实现「人才库」这一屏的主流程（列表 / 筛选 / 详情 / 改档 / 推进阶段 / 头像）。
   邮件、管道、配置等页面仍走旧版。确认方向后再谈替换。
"""
from __future__ import annotations

import hashlib
import json
import re

from .ui_v2_css import _CSS
from .ui_v2_js import _JS

from . import stages as stages_mod
from . import tiers as tiers_mod

# 占位符只出现一次，用 replace 注入（不用 f-string，避免 JS 里的大括号要转义）。
_PH_STAGES = "__V2_STAGES__"
_PH_TIERS = "__V2_TIERS__"
#: 登录态开关（2026-10-20）：v2 之前不注入，前端因此从不带 X-TP-Token，
#: 设了 TP_AUTH=1 时全页接口 401。现在与原版同一套口径。
_PH_AUTH = "__V2_AUTH__"

_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>人才库智能体</title>
<!-- 标签页图标：复用侧栏品牌图，避免浏览器去请求 /favicon.ico（404） -->
<link rel="icon" type="image/png" href="__BRAND_ICON__">
<style>__V2_CSS_BLOCK__</style>
<script>
(function(){
  try{
    var t = localStorage.getItem('tp_theme');
    if(t === 'dark' || t === 'light'){ document.documentElement.setAttribute('data-theme', t); }
  }catch(e){}
})();
</script>
</head>
<body>
<div class="loadbar" id="loadBar" aria-hidden="true"></div>
<aside class="side">
  <!-- 品牌区：应用图标 + 产品名。原来的写法是一个 9px 蓝色小方块 + "人才库"，
       那个方块是占位符（`.sbrand i`），跟 exe 图标毫无关系；而"人才库"又是
       下面第一个导航项的名字，顶上一遍、导航里一遍，读起来像重复了。
       现在换成真正的应用图标 + 产品名 + 一行说明（业务方 2026-10-27）。 -->
  <div class="sbrand">
    <img src="__BRAND_ICON__" alt="">
    <!-- 副标题「西北有色院 · 人事」已删（2026-10-28）：产品名本身已经说清是什么，
         这一行在窄侧栏里只会折成两行小字。 -->
    <span><b>人才库智能体</b></span>
  </div>
  <!-- 智能助手是全局入口，不属于某个业务组，放在品牌下方最上面。 -->
  <button data-view="chat" class="on">
    <svg class="ico" viewBox="0 0 24 24"><path d="M20.5 12a8.5 8.5 0 0 1-12.2 7.6L3.5 20.5l.9-4.8A8.5 8.5 0 1 1 20.5 12Z"/><path d="M8.6 11.8h.01M12 11.8h.01M15.4 11.8h.01"/></svg>
    <span>智能助手</span></button>
  <div class="sgroup">招聘</div>
  <button data-view="pool">
    <svg class="ico" viewBox="0 0 24 24"><circle cx="9" cy="8" r="3.2"/><path d="M3.6 19.2c.5-3.3 2.7-5.2 5.4-5.2s4.9 1.9 5.4 5.2"/><path d="M16.2 5.6a3 3 0 0 1 0 5.9"/><path d="M17.6 19.2c-.2-2.3-1-4-2.4-4.9 2.7-.3 5.1 1.5 5.4 4.9"/></svg>
    <span>人才库</span></button>
  <button data-view="schedule">
    <svg class="ico" viewBox="0 0 24 24"><rect x="3.2" y="5" width="17.6" height="15" rx="2"/><path d="M7.5 3.5v3M16.5 3.5v3M3.2 9.5h17.6"/><path d="M7.5 13h.01M12 13h.01M16.5 13h.01M7.5 16.5h.01M12 16.5h.01"/></svg>
    <span>日程安排</span></button>
  <button data-view="mail">
    <svg class="ico" viewBox="0 0 24 24"><rect x="3" y="5.5" width="18" height="13" rx="2.2"/><path d="m3.8 7.2 7.1 5.2a2 2 0 0 0 2.2 0l7.1-5.2"/></svg>
    <span>写邮件</span></button>
  <button data-view="recruit">
    <svg class="ico" viewBox="0 0 24 24"><rect x="3.2" y="4.5" width="17.6" height="15" rx="2"/><path d="M3.2 9.2h17.6M8.4 9.2v10.3M15.6 9.2v10.3"/><path d="M7 6.8h.01M10.2 6.8h.01"/></svg>
    <span>招聘进度</span></button>
  <button data-view="org">
    <svg class="ico" viewBox="0 0 24 24"><rect x="3" y="7.5" width="18" height="12" rx="2.2"/><path d="M9 7.5V6a1.8 1.8 0 0 1 1.8-1.8h2.4A1.8 1.8 0 0 1 15 6v1.5"/><path d="M3 12.2h18"/></svg>
    <span>岗位管理</span></button>
  <button data-view="archive">
    <svg class="ico" viewBox="0 0 24 24"><rect x="3.2" y="4.5" width="17.6" height="4.4" rx="1.4"/><path d="M4.8 8.9V18a1.8 1.8 0 0 0 1.8 1.8h10.8A1.8 1.8 0 0 0 19.2 18V8.9"/><path d="M9.6 12.4h4.8"/></svg>
    <span>归档</span></button>
  <!-- 账号管理/系统配置不再占左侧导航：设置从账号行右侧齿轮打开。 -->
  <!-- 构建时间由服务端注入：改了代码后如果这里的时间没变，说明浏览器还在跑旧页面 -->
  <div class="sfoot" id="sfoot">
    <button class="acct" id="acctBtn" onclick="toggleAcct(event)"
            title="账号" aria-haspopup="true" aria-expanded="false">
      <span class="av" id="acctAv">·</span>
      <span class="txt"><b id="acctName">本机模式</b><i id="acctRole"></i></span>
      <span class="chev">▴</span>
    </button>
    <button class="acctgear" id="acctSettings" onclick="openSettings('model')"
            title="设置" aria-label="设置">
      <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3.1"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.83 2.83-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.03 1.56V21h-4v-.09A1.7 1.7 0 0 0 8.97 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.83-2.83.06-.06A1.7 1.7 0 0 0 4.6 15a1.7 1.7 0 0 0-1.56-1.03H3v-4h.09A1.7 1.7 0 0 0 4.6 8.97a1.7 1.7 0 0 0-.34-1.88l-.06-.06 2.83-2.83.06.06A1.7 1.7 0 0 0 8.97 4.6 1.7 1.7 0 0 0 10 3.04V3h4v.09a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.88-.34l.06-.06 2.83 2.83-.06.06A1.7 1.7 0 0 0 19.4 8.97 1.7 1.7 0 0 0 20.96 10H21v4h-.09A1.7 1.7 0 0 0 19.4 15Z"/></svg>
    </button>
    <div class="acctMenu" id="acctMenu" role="menu">
      <button role="menuitem" onclick="acctGo('users')" id="acctUsers" hidden>账号管理</button>
      <button role="menuitem" onclick="toggleTheme()" id="acctTheme">深色模式</button>
      <button role="menuitem" onclick="doLogout()" id="acctLogout" hidden>退出登录</button>
      <div class="ver">__V2_BUILD__</div>
    </div>
  </div>
</aside>

<div class="main">
<div class="top" id="topBar">
  <!-- 窄屏（<=1100px）才显示：把左栏名单抽成抽屉，详情占满宽度 -->
  <button class="btn" id="btnListToggle" onclick="toggleListDrawer()"
          aria-label="打开候选人名单">名单</button>
  <div class="brand" style="display:none"><i></i>人才库</div>
  <input id="kw" class="search" placeholder="搜索姓名 / 院校 / 专业 / 技能"
         onkeydown="if(event.key==='Enter'){doSearch()}">
  <!-- 搜索就是**一个输入框 + 一个按钮**，与原版一致（`ui.py:1787-1793`）：
       原来这里有个「关键词 / 按技能 / 语义」下拉，还有一个「具备其一/全部具备」下拉 —
       三个控件在管同一件事，业务方 2026-10-21 明确要求换成一个搜索按钮。
       关键词本身就能命中姓名/院校/专业/技能，够用。 -->
  <button class="btn primary" onclick="doSearch()" style="height:34px;padding:0 20px">搜索</button>
  <!-- 学历/四六级用**勾选式下拉**（业务方 2026-10-21：原生 multiple 太丑，要"都是下拉框"）。
       外观与旁边的单选框一致，点开是带复选框的浮层，仍支持多选。 -->
  <div class="msel" id="msEdu">
    <input type="hidden" id="fEdu">
    <button type="button" class="sel msbtn" onclick="msToggle('msEdu')">学历：不限 ▾</button>
    <div class="msPanel"></div>
  </div>
  <select id="fStage" class="sel"><option value="">阶段：不限</option></select>
  <!-- 档位不在这里再放一个下拉：上面的频道 tab 已经筛同一件事，
       两个入口筛同一个集合只会让人怀疑"为什么筛出来的不一样"。 -->
  <div class="more">
    <button class="btn" id="btnMore" onclick="toggleMore()">更多筛选 ▾</button>
    <div class="morePanel" id="morePanel">
      <select id="fGender" class="sel"><option value="">性别：不限</option></select>
      <select id="fUniv" class="sel"><option value="">院校层次：不限</option></select>
      <select id="fBachelor" class="sel"><option value="">第一学历：不限</option></select>
      <!-- 海外院校是**独立维度**（海外高校既不是 985 也不是 211），
           三态：海外 / 非海外 / 未识别（学校没读到，要人工补录）。原版有，v2 缺。 -->
      <select id="fOverseas" class="sel"><option value="">海外院校：不限</option></select>
      <div class="msel" id="msCet">
        <input type="hidden" id="fCet">
        <button type="button" class="sel msbtn" onclick="msToggle('msCet')">四六级：不限 ▾</button>
        <div class="msPanel"></div>
      </div>
      <!-- 岗位筛选（v5.1）：按归岗关系筛选；「未归岗」是特殊值，后端按 job_id IS NULL 处理 -->
      <select id="fJob" class="sel"><option value="">岗位：不限</option><option value="__unassigned__">未归岗</option></select>
      <!-- 面试部门（原版有、v2 缺）：后端 `interview_unit` 支持顿号/逗号分隔的多值匹配 -->
      <select id="fUnit" class="sel"><option value="">面试部门：不限</option></select>
      <select id="fOutcome" class="sel"><option value="">面试结论：不限</option></select>
      <!-- 投递时间改成**具体日期的起止范围**（业务方 2026-11-02）：
           原来的「近 7 天 / 近 30 天 / 今年」只能选粗档，筛不到"某月某日到某月某日"。
           后端 `db.search_candidates` 本来就支持 `date_from` / `date_to`（按 applied_at，
           含结束当天），这里只换控件，查询链路不变。
           输入框平时是 text，点开才切成 date —— 这样能显示"开始日期/结束日期"占位，
           原生 date 控件的占位是固定的 yyyy/mm/dd，改不了。 -->
      <div class="dateRange" id="fDateRange">
        <span class="lbl">投递时间</span>
        <input id="fDateFrom" type="text" placeholder="开始日期"
               onfocus="this.type='date'" onblur="if(!this.value)this.type='text'">
        <span class="dash">~</span>
        <input id="fDateTo" type="text" placeholder="结束日期"
               onfocus="this.type='date'" onblur="if(!this.value)this.type='text'">
      </div>
      <div class="clearRow"><button class="btn" onclick="clearFilters()">清空筛选</button></div>
    </div>
  </div>
  <button class="btn" id="btnOverview" onclick="showOverview()">概览 · 待办</button>
  <!-- 当前登录者已移到**左下角账号区**（2026-10-30 业务方要求），顶部不再重复显示 -->
  <div class="count" id="count" aria-live="polite"></div>
</div>

<div class="top2">
  <button class="btn btn-primary btn-lg" id="btnIngestMail" onclick="doIngest('mailbox')">收取邮箱简历</button>
  <button class="btn btn-primary btn-lg" id="btnIngestDir" onclick="doIngest('folder')">导入本地文件夹</button>
  <button class="btn btn-primary btn-lg" id="btnUpload" onclick="document.getElementById('upFile').click()"
          title="手机拍的纸质简历照片、扫描件，直接选文件上传入库（不用先改配置目录）">上传简历照片/文件</button>
  <input id="upFile" type="file" multiple hidden
         accept=".jpg,.jpeg,.png,.bmp,.webp,.tif,.tiff,.pdf,.docx,.doc,.txt" onchange="doUpload(this.files)">
  <span class="vdiv"></span>
  <button class="btn btn-green btn-lg" onclick="doExport()">导出名单</button>
  <button class="btn btn-green btn-lg" onclick="doBundle()">下载初筛简历</button>
  <span class="vdiv"></span>
  <label class="allbox" title="勾选当前筛选条件下的全部人（含其他页）"><input type="checkbox" id="chkAll"> 全选筛选结果</label>
  <!-- v4.3.0：批量初筛通过 + 批量归岗收进「批量操作」菜单 -->
  <div class="more" id="moreBatchOps">
    <button class="btn btn-primary btn-lg" id="btnBatchOps" onclick="toggleBatchOpsMenu()">批量操作 ▾</button>
    <div class="morePanel" id="batchOpsPanel">
      <button class="btn" id="btnInitial" onclick="toggleBatchOpsMenu();initialPassChecked()" disabled>批量初筛通过（0）</button>
      <button class="btn" id="btnBulkAssign" onclick="toggleBatchOpsMenu();bulkAssignChecked()" disabled>批量归岗（0）</button>
      <button class="btn" onclick="toggleBatchOpsMenu();batchReanalyze()">批量重新分析</button>
      <button class="btn" onclick="toggleBatchOpsMenu();analyzePending()">分析未分析的人</button>
    </div>
  </div>
  <button class="btn btn-lg" id="btnArch" onclick="archiveChecked()" disabled>归档勾选（0）</button>
  <button class="btn btn-lg" id="btnCompare" onclick="compareChecked()" disabled>对比档案（0/10）</button>
  <button class="btn btn-lg" id="btnClearSel" onclick="clearSel()" hidden title="清空所有已勾选的人（含不在当前筛选结果里的）">清空选择</button>
  <span class="hint" id="selHint" style="display:none"></span>
  <span class="vdiv"></span>
  <button class="btn btn-lg" id="btnPipe" onclick="togglePipe()">投递管道 ▾</button>
  <!-- 「超期设置」已挪进管道面板右上角（业务方 2026-10-21）：
       它只影响管道看板里"停留超 N 天"的标红，是管道自己的参数，
       放在工具栏里既占位置、也让人以为它管的是别的东西。 -->
  <span class="vdiv"></span>
  <button class="btn btn-primary btn-lg" id="btnProp" onclick="toggleProps()">待确认提案</button>
</div>
<div class="tabbar"><div class="tabs" id="tabs"></div></div>

<div id="mailView"></div>
<div id="orgView"></div>
<div id="sysView"></div>
<div id="usersView"></div>
<div id="chatView"></div>
<div id="recruitView"></div>
<div id="scheduleView"></div>
<div id="archView"></div>

<div class="pipe" id="pipe"></div>
<div class="propbox" id="propBox"></div>

<div class="listmask" id="listMask" onclick="toggleListDrawer()"></div>
<div class="wrap" id="wrapBox">
  <div class="listwrap">
  <div class="list" id="list"></div>
  <div class="pager" id="pager"></div>
</div>
  <div class="detail" id="detail"><div class="hint">读取概览…</div></div>
</div>
</div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
<div class="mask" id="mask"><div class="dlg" role="dialog" aria-modal="true"><h3 id="dlgTitle"></h3><div class="db" id="dlgBody"></div><div class="db" id="edDlgBody" style="display:none"></div><div class="dbar"><button class="btn" id="dlgNo"></button><button class="btn primary" id="dlgYes"></button></div></div></div>
<div class="settingsmask" id="settingsMask" onclick="if(event.target===this)closeSettings()">
  <div class="settingsdlg" role="dialog" aria-modal="true" aria-label="设置">
    <button class="settingsclose" type="button" onclick="closeSettings()" title="关闭">×</button>
    <div class="settingsnav" id="settingsNav"></div>
    <div class="settingsbody" id="settingsBody"></div>
  </div>
</div>

<!-- 登录页（v3.0.3）。只有 AUTH_ENABLED=true 时才显示；本机模式整块不出现。
     刻意做成**全屏遮罩**而不是独立路由：这个页面本身是单文件内联的，
     多一个 URL 就多一处要跟缓存/刷新较劲的地方。 -->
<div class="loginwrap" id="loginWrap" hidden>
  <!-- 人才星图（v3.2）：canvas 必须在 .loginbox **之前**，这样它自然落在下面；
       CSS 里再给它 z-index:0 / 登录框 z-index:1 兜一层。 -->
  <canvas id="loginNet" aria-hidden="true"></canvas>
  <form class="loginbox" id="loginForm" autocomplete="on">
    <div class="loginbrand"><i class="logindot"></i>人才库智能体</div>
    <!-- 副标题「西北有色院 · 人事」已删（2026-10-28）：同一句话不再出现在界面上 -->
    <label class="loginlabel" for="loginUser">账号</label>
    <input id="loginUser" class="logininput" autocomplete="username" required>
    <label class="loginlabel" for="loginPass">密码</label>
    <input id="loginPass" class="logininput" type="password" autocomplete="current-password" required>
    <div class="loginerr" id="loginErr"></div>
    <!-- 忙碌/成功图标（v3.2）：同一条 SVG，圆环转 → 对勾画出来。
         平时 display:none，只占 14px，不参与布局计算。 -->
    <button class="btn primary loginbtn" type="submit" id="loginBtn">
      <svg class="loginico" viewBox="0 0 16 16" aria-hidden="true">
        <circle cx="8" cy="8" r="6"></circle>
        <path d="M4.6 8.3l2.3 2.3 4.5-4.8"></path>
      </svg>
      <span id="loginBtnText">登录</span>
    </button>
    <!-- 首次使用提示（v3.1.3）：原来这里只有"忘记密码请联系管理员"，
         而**管理员自己也不知道初始密码是什么** —— 双击打开、看到登录页、
         输不进去，第一反应是"程序坏了"。首次部署必须把默认账号写出来。
         密码改过之后这行会自动消失（服务端按 must_change_password 决定是否下发）。 -->
    <div class="loginhint" id="loginFirstHint" hidden></div>
    <div class="loginhint">忘记密码请联系系统管理员重置。</div>
  </form>
</div>

<script>
/* 这两份配置必须按请求注入（每个部署阶段/档位不同），所以留在内联；
   主 JS/CSS 在下方拆成带内容 hash 的静态资源，可长期缓存。 */
var STAGE_CFG = __V2_STAGES__;
var TIER_CFG  = __V2_TIERS__;
var AUTH_ENABLED = __V2_AUTH__;
</script>
<script id="ui-main">__V2_JS_BLOCK__</script>
</body>
</html>
"""

# ---------------------------------------------------------------- 静态资源拆分
# 页面原来是一整块约 500KB 的内联 CSS/JS，响应又是 no-store —— 每次刷新都要
# 重新下载并解析。这里在模块加载时把两块抽出来，用内容 hash 做文件名，
# 由 /ui/<hash>.css|js 长期缓存；HTML 里只保留按请求注入的阶段/档位/登录开关。
_CSS_NAME = "ui-%s.css" % hashlib.sha1(_CSS.encode("utf-8")).hexdigest()[:12]
_JS_NAME = "ui-%s.js" % hashlib.sha1(_JS.encode("utf-8")).hexdigest()[:12]
_ASSETS = {
    _CSS_NAME: (_CSS, "text/css; charset=utf-8"),
    _JS_NAME: (_JS, "application/javascript; charset=utf-8"),
}
_PAGE = _PAGE.replace(
    "<style>__V2_CSS_BLOCK__</style>",
    '<link rel="stylesheet" href="/ui/%s">' % _CSS_NAME, 1,
)
_PAGE = _PAGE.replace(
    '<script id="ui-main">__V2_JS_BLOCK__</script>',
    '<script src="/ui/%s"></script>' % _JS_NAME, 1,
)


def get_ui_asset(name: str):
    """返回 (body, media_type)；文件名不匹配（含旧 hash）返回 None。"""
    return _ASSETS.get(name)


def render_page_v2(auth_enabled: bool = False) -> str:
    """渲染 v2 预览页（注入阶段/档位配置，与旧版同一份真源）。"""
    stages_json = json.dumps({
        "order": stages_mod.all_stages(),
        "default_stage": stages_mod.DEFAULT_STAGE,
        "colors": {s: stages_mod.stage_color(s) for s in stages_mod.all_stages()},
        # v2.19：结论标签是**按阶段限定**的（待面试/已面试→拟录用·不录用，已发放offer→已签约·拒签），
        # 而「拟录用/不录用/已签约」这些结论在原版里是**阶段动作**的一部分。
        # 不注入 actions/outcomes，界面就只能给出一个固定的、未判/拟录用/不录用 下拉 ——
        # 那正是"改了不生效"的根因：服务端按阶段校验值域，前端给的选项压根不合法。
        "outcomes": (stages_mod.load_cfg().get("outcomes") or {}),
        "actions": (stages_mod.load_cfg().get("actions") or {}),
    }, ensure_ascii=False)
    tiers_json = json.dumps({
        "order": tiers_mod.all_tiers(),
        "labels": tiers_mod.labels(),
    }, ensure_ascii=False)
    # 构建标记：页脚显示"v2 预览 · 10-21 15:40 · js-ab12cd34"。
    # 不能只看本文件 mtime —— 只改 JS/CSS 时页脚时间不变，HR 会以为浏览器
    # 还在跑旧前端。取三个源文件的最大 mtime，并附上内容 hash 短码。
    import datetime as _dt
    import os as _os
    _mtimes = []
    for _p in (__file__,
               _os.path.join(_os.path.dirname(__file__), "ui_v2_js.py"),
               _os.path.join(_os.path.dirname(__file__), "ui_v2_css.py")):
        try:
            _mtimes.append(_os.path.getmtime(_p))
        except OSError:
            continue
    if _mtimes:
        _build = (_dt.datetime.fromtimestamp(max(_mtimes)).strftime("%m-%d %H:%M")
                  + " · " + _JS_NAME[3:11])
    else:
        # PyInstaller 把模块放进 PYZ 时 __file__ 是虚拟路径，不能拿来 stat。
        from .version import VERSION

        _build = f"v{VERSION} · {_JS_NAME[3:11]}"
    html = _PAGE.replace(_PH_STAGES, stages_json).replace(_PH_TIERS, tiers_json) \
            .replace(_PH_AUTH, "true" if auth_enabled else "false") \
            .replace("__BRAND_ICON__", _brand_icon_data_uri()) \
            .replace("__V2_BUILD__", _build)
    return html


def _brand_icon_data_uri() -> str:
    """侧栏品牌图标：icon_tray.png 缩到 96px 的 base64 data URI。

    为什么内联而不是挂个 /logo.png：
      · 页面本来就是单文件内联的，多一个请求在服务刚起时还会闪一下空图；
      · 打包后程序目录是 `_internal`，多一个"要跟着 spec 一起带上"的静态资源，
        就多一个会漏的地方（图标丢了下场是页面上一个裂图）。
    找不到图就退回空串——`<img src="">` 只会显示 alt（空），布局不塌。

    结果按 `(路径, mtime_ns, size)` 缓存：原来每次渲染首页都重新打开、
    PIL 缩放并 base64 一遍（20-50ms CPU + 一次图片解码），而图标文件几乎不变。
    """
    import base64
    import io
    import os as _os

    from .paths import BASE

    # 三个候选位置，覆盖三种跑法：
    #   BASE/                —— 源码开发时把图标放在 repo_src 下
    #   BASE 的上一级        —— 源码开发时图标在工作区根（当前就是这种）
    #   BASE/_internal/      —— 打包后 BASE 已经是 _internal，图标跟 exe 同级
    here = _os.path.dirname(BASE)
    png = ""
    for cand in (_os.path.join(BASE, "icon_tray.png"),
                 _os.path.join(here, "icon_tray.png"),
                 _os.path.join(BASE, "_internal", "icon_tray.png")):
        if _os.path.isfile(cand):
            png = cand
            break
    if not png:
        return _BRAND_ICON_FALLBACK
    try:
        _st = _os.stat(png)
        _key = (png, _st.st_mtime_ns, _st.st_size)
    except OSError:
        _key = (png, 0, 0)
    _cached = _BRAND_ICON_CACHE.get(_key)
    if _cached is not None:
        return _cached
    try:
        from PIL import Image

        with Image.open(png) as im:
            im = im.convert("RGBA")
            if im.size[0] > 96:
                im = im.resize((96, 96), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, format="PNG", optimize=True)
        out = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        # 缩不了就直接内联原图：宁可页面大 50KB，也不要品牌区空一块。
        try:
            with open(png, "rb") as fh:
                out = "data:image/png;base64," + base64.b64encode(fh.read()).decode("ascii")
        except OSError:
            out = _BRAND_ICON_FALLBACK
    # 只保留最近一份，图标文件替换后旧 key 自然失效。
    _BRAND_ICON_CACHE.clear()
    _BRAND_ICON_CACHE[_key] = out
    return out


#: 品牌图标 data URI 的进程内缓存：key=(路径, mtime_ns, size)。
_BRAND_ICON_CACHE: dict[tuple, str] = {}


#: 品牌图标的**兜底**（v3.1.4）：一个内联 SVG，不依赖任何文件或 PIL。
#: 为什么必须有它：现场出现过侧栏左上角显示"裂图"——`<img src="">` 在浏览器里
#: 就是裂图占位符，比没有图标更难看。现在无论 PNG 能不能读出来，
#: 最差也是这个蓝色圆角方块 + 白色 "H"，布局和视觉都不会塌。
_BRAND_ICON_FALLBACK = (
    "data:image/svg+xml;charset=utf-8,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 96 96'%3E"
    "%3Crect width='96' height='96' rx='22' fill='%23005bdf'/%3E"
    "%3Cpath d='M26 70V26h10v17h24V26h10v44H60V53H36v17z' fill='%23fff'/%3E"
    "%3C/svg%3E"
)
