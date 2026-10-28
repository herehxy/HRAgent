from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
source = (ROOT / "app" / "ui_v2.py").read_text(encoding="utf-8")
start = source.index("var ANA = {")
end = source.index("async function anaLoad()", start)
body = source[start:end]
harness = r"""
const box = {innerHTML: ""};
const scope = {textContent: ""};
global.document = {
  getElementById(id) {
    if (id === "anaCustom") return box;
    if (id === "anaScope") return scope;
    return null;
  }
};
global.localStorage = {
  getItem() { return null; },
  setItem() {}
};
global.esc = v => String(v == null ? "" : v);
global.jq = v => JSON.stringify(v);
global.toast = () => {};
global.clearFilters = () => {};
global.STAGE_CFG = {colors: {全部简历: "#333", 已面试: "#090"}, order: []};
"""
test = r"""
ANA.items = [
  {stage: "全部简历", edu_level: "本科", gender: "男", interview_unit: "",
   job_title: "岗位A", channel: "文件夹", skills: ["Python"], email: "a@x.com",
   resume_doc_id: 1, needs_review: false, uni_tier: "985", cet_level: "六级"},
  {stage: "已面试", edu_level: "硕士", gender: "女", interview_unit: "测试中心",
   job_title: "岗位B", channel: "邮箱", skills: ["Java"], email: "b@x.com",
   resume_doc_id: 2, needs_review: true, uni_tier: "211", cet_level: "四级"}
];
ANA.dim = "stage";
ANA.conds = {};
anaRenderCustom();
if (!box.innerHTML.includes("anacondhead")) throw new Error("条件区未渲染");
if (!box.innerHTML.includes("barchart")) throw new Error("图表未渲染");
if (!box.innerHTML.includes("brow")) throw new Error("柱状图数据未渲染");
console.log(JSON.stringify({ok: true, html_length: box.innerHTML.length}));
"""
with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                 encoding="utf-8") as fh:
    fh.write(harness + body + test)
    path = fh.name
try:
    subprocess.run(["node", path], check=True)
finally:
    Path(path).unlink(missing_ok=True)
