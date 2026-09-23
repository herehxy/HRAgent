"""领域包（domain pack）：让「新增一个行业的岗位」不再需要改代码。

## 为什么需要它

改造前的扩展方式是"人手往 `config/ontology.json` 里加技能"——加完还要跑
`sync-ontology`、`refresh-skills`，是一条开发流程。于是每来一个新领域的岗位
（财务、人力、法务……），都得找开发。**做不到"HR 自己就能上一个新领域"。**

领域包把这件事变成一次**数据导入**：

    cli.py import-domain config/domains/财务.json

一个 JSON 声明"这个行业常用哪些技能（含别名、归哪个技能大类）"，
导入时合并进技能本体、刷新库内分类、写审计，并**报告冲突**。

## 设计约束（为什么这样合并）

1. **只增不删**：从不删除本体里已有的条目。导入是加法，误导入可以靠
   `data/backup/ontology-*.json` 回到导入前。
2. **别名冲突要报告、不静默**：同一个写法在两个条目下都声明了别名时，
   以**本次导入的条目为准**（导入是显式动作），但必须把"从 A 挪到 B"
   记进返回结果的 `aliases_moved`，否则分类口径会被悄悄改掉。
   例：本体里 `成本核算` 曾经是 `成本控制` 的别名，财务包把它收归自己名下。
3. **技能大类不轻易新增**：大类是方向判定的维度，随便加会让口径碎掉。
   包里要用新大类必须在 `new_categories` 里显式声明，导入结果里单独列出。
"""
from __future__ import annotations

import glob
import json
import os
import shutil
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ONTOLOGY_PATH = os.path.join(ROOT, "config", "ontology.json")
MAJORS_PATH = os.path.join(ROOT, "config", "majors.json")
DOMAIN_DIR = os.path.join(ROOT, "config", "domains")
BACKUP_DIR = os.path.join(ROOT, "data", "backup")


def pack_dir() -> str:
    return DOMAIN_DIR


def list_packs() -> list[dict]:
    """列出可用领域包（含技能数、涉及的技能大类）。"""
    out: list[dict] = []
    for path in sorted(glob.glob(os.path.join(DOMAIN_DIR, "*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception as exc:  # 坏文件不该让整个列表挂掉
            out.append({"file": os.path.basename(path), "name": os.path.basename(path)[:-5],
                        "error": f"{type(exc).__name__}: {exc}"})
            continue
        skills = data.get("skills") or []
        cats = sorted({s.get("category") or "其他" for s in skills})
        out.append({
            "file": os.path.basename(path),
            "name": data.get("name") or os.path.basename(path)[:-5],
            "description": data.get("description") or "",
            "version": data.get("version"),
            "skill_count": len(skills),
            "categories": cats,
            "new_categories": list(data.get("new_categories") or []),
            "major_count": len(data.get("majors") or []),
        })
    return out


def load_pack(path_or_name: str) -> dict:
    """按文件名或领域名读领域包。"""
    p = path_or_name
    if not os.path.isabs(p) and not os.path.exists(p):
        cand = os.path.join(DOMAIN_DIR, p)
        if os.path.exists(cand):
            p = cand
        elif os.path.exists(cand + ".json"):
            p = cand + ".json"
        else:
            for f in glob.glob(os.path.join(DOMAIN_DIR, "*.json")):
                with open(f, encoding="utf-8") as fh:
                    d = json.load(fh)
                if (d.get("name") or "") == path_or_name:
                    p = f
                    break
    with open(p, encoding="utf-8") as fh:
        data = json.load(fh)
    data["_path"] = p
    return data


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _backup(path: str, tag: str) -> str:
    """备份到 data/backup/，**文件名必须唯一**。

    踩过：连续导入两个领域包时 `_stamp()` 只到秒，两个备份同名，
    后一次把前一次覆盖掉了——于是"连续导入两次"之后就回不到最初始的状态。
    备份这东西只在出事时才被用，绝不能出现"看起来有、实际被覆盖"。
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    base = os.path.join(BACKUP_DIR, f"{tag}-{_stamp()}")
    dst = base + ".json"
    n = 1
    while os.path.exists(dst):
        n += 1
        dst = f"{base}-{n}.json"
    shutil.copy2(path, dst)
    return dst


def _bump(version: str | None) -> str:
    """1.1 → 1.2（小数位自增；非法格式原样加后缀，不猜）。"""
    try:
        major, minor = str(version or "1.0").split(".")
        return f"{major}.{int(minor) + 1}"
    except Exception:
        return f"{version or '1.0'}+"


def preview_import(pack_path_or_name: str) -> dict:
    """只算不改：告诉调用方这次导入会新增什么、会动哪些别名。"""
    pack = load_pack(pack_path_or_name)
    with open(ONTOLOGY_PATH, encoding="utf-8") as fh:
        onto = json.load(fh)
    return _merge(onto, pack, dry_run=True)


def _merge(onto: dict, pack: dict, dry_run: bool = False) -> dict:
    skills = onto.get("skills") or []
    cats = list(onto.get("categories") or [])
    by_canon = {s.get("canonical"): s for s in skills if s.get("canonical")}

    alias_owner: dict[str, str] = {}
    for s in skills:
        for a in (s.get("aliases") or []):
            alias_owner.setdefault(str(a), str(s.get("canonical")))

    added, merged, moved, cats_added = [], [], [], []
    for item in pack.get("skills") or []:
        canon = (item.get("canonical") or "").strip()
        if not canon:
            continue
        cat = (item.get("category") or "").strip() or "其他"
        aliases = [str(a).strip() for a in (item.get("aliases") or []) if str(a).strip()]
        if cat not in cats and cat not in cats_added:
            cats_added.append(cat)
        # 条目名本身也可能正被别人当别名用（财务包的 `成本核算` 原本挂在 `成本控制` 名下）。
        # 名字的归属优先级最高：先把它从别人名下摘出来，并如实报告。
        owner = alias_owner.get(canon)
        if owner and owner != canon and owner in by_canon:
            old = by_canon[owner]
            old["aliases"] = [x for x in (old.get("aliases") or []) if x != canon]
            moved.append({"alias": canon, "from": owner, "to": canon})
        cur = by_canon.get(canon)
        if cur is None:
            entry = {"canonical": canon, "category": cat, "aliases": aliases}
            skills.append(entry)
            by_canon[canon] = entry
            added.append(canon)
        else:
            own = list(cur.get("aliases") or [])
            new_aliases = [a for a in aliases if a not in own]
            if new_aliases:
                cur["aliases"] = own + new_aliases
                merged.append({"canonical": canon, "aliases_added": new_aliases})
        # 别名归属：同一个写法被两个条目声明时，本次导入显式生效，但要报告
        for a in aliases:
            owner = alias_owner.get(a)
            if owner and owner != canon and owner in by_canon:
                old = by_canon[owner]
                old["aliases"] = [x for x in (old.get("aliases") or []) if x != a]
                moved.append({"alias": a, "from": owner, "to": canon})
            alias_owner[a] = canon
        alias_owner[canon] = canon

    result = {
        "pack": pack.get("name"),
        "file": os.path.basename(pack.get("_path") or ""),
        "skills_added": added,
        "skills_merged": merged,
        "aliases_moved": moved,
        "categories_added": cats_added,
        "major_added": [],
        "version_before": onto.get("version"),
        "version_after": _bump(onto.get("version")),
        "dry_run": dry_run,
    }
    if dry_run:
        return result

    onto["categories"] = cats + [c for c in cats_added if c not in cats]
    onto["version"] = result["version_after"]
    prior = onto.get("note") or ""
    onto["note"] = (prior + f"\n[{datetime.now():%Y-%m-%d %H:%M}] 导入领域包「{pack.get('name')}」："
                             f"新增 {len(added)} 条、合并 {len(merged)} 条、"
                             f"别名迁移 {len(moved)} 条。").strip()
    result["backup"] = _backup(ONTOLOGY_PATH, "ontology")
    with open(ONTOLOGY_PATH, "w", encoding="utf-8") as fh:
        json.dump(onto, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    # 领域包也可以补学科目录（同一个"只加数据"的思路）
    majors = pack.get("majors") or []
    if majors:
        with open(MAJORS_PATH, encoding="utf-8") as fh:
            mj = json.load(fh)
        have = {m.get("name") for m in (mj.get("majors") or [])}
        mj.setdefault("majors", [])
        for m in majors:
            if (m.get("name") or "").strip() and m["name"] not in have:
                mj["majors"].append({"name": m["name"],
                                     "category": m.get("category") or "交叉学科",
                                     "aliases": list(m.get("aliases") or [])})
                result["major_added"].append(m["name"])
        if result["major_added"]:
            result["majors_backup"] = _backup(MAJORS_PATH, "majors")
            mj["version"] = _bump(mj.get("version"))
            with open(MAJORS_PATH, "w", encoding="utf-8") as fh:
                json.dump(mj, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
    return result


def import_pack(conn, pack_path_or_name: str, operator: str = "HR") -> dict:
    """导入领域包：合并进技能本体 → 刷新库内分类 → 写审计。

    刷新这一步不能省：`upsert_skill` 对已存在的技能只合并别名、不更新分类，
    所以本体改了分类之后，库里的老条目会停在旧分类，方向判定会继续按旧口径算。
    """
    from .. import db
    from . import majors, normalize as nz

    pack = load_pack(pack_path_or_name)
    with open(ONTOLOGY_PATH, encoding="utf-8") as fh:
        onto = json.load(fh)
    result = _merge(onto, pack, dry_run=False)

    nz.load_ontology(reload=True)
    majors.load(reload=True)
    sync = db.sync_skill_categories(conn, nz.ontology_categories())
    result["synced"] = sync
    result["ontology_after"] = nz.describe()
    result["majors_after"] = majors.describe()
    db.add_audit(conn, "ontology", str(result.get("version_after")), "import_domain",
                 json.dumps({"version": result["version_before"]}, ensure_ascii=False),
                 json.dumps({"pack": result["pack"],
                             "added": len(result["skills_added"]),
                             "merged": len(result["skills_merged"]),
                             "aliases_moved": len(result["aliases_moved"])},
                            ensure_ascii=False),
                 operator, "hr")
    return result
